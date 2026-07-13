"""Deprecation-expiry ledger — a strict-xfail punch list of time-boxed shims.

The tree carries several *time-boxed* compat shims that each promise their own
removal ("remove once no mid-flight run predates the fix", "for one release",
"kept for ~20 legacy test files", "removed in a future release"). Nothing made
those promises enforceable, so an outlived shim just lingers.

This ledger enumerates each live shim together with the mechanical condition
that ends its window. It borrows the strict-xfail *punch-list* idiom from
``tests/contracts/test_recovery_registry.py``: every entry is a dynamic
``xfail`` while the shim is legitimately still in its window, and flips to a
loud ``fail`` (strict-xpass) the moment its expiry condition fires. So:

* while a shim is within its window → the row ``xfails`` (suite stays green);
* once the shim has *outlived* its window → the row ``fails`` with the exact
  removal instruction, and stays red until the shim (and this row) are deleted;
* if a shim is removed but its ledger row is left behind → the presence guard
  ``fails`` (the source marker is gone), forcing the stale row out.

The bidirectional enforcement mirrors ``test_recovery_registry.py``'s
``all_kinds()`` ⇔ ``PORTED_KINDS`` diff: neither the shim nor its ledger row
can drift away from the other silently.

Relationship to ``test_backcompat_expiry.py``: that test is the *hard-assert*
half (a version-target graveyard — "substring must be ABSENT once version >=
X"). This file is the *strict-xfail live-shim* half — it tracks shims that are
still present and legitimately in-window, and it supports non-version expiry
triggers (a calendar sunset, a migration-count drain) that a pure version
target cannot express. The two do not share entries.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date
from importlib.resources import files as _resource_files
from pathlib import Path

import pytest

from hpc_agent import __version__

# ── source-tree anchors ────────────────────────────────────────────────────
# The installed package (matches ``test_backcompat_expiry.py``'s resource
# lookup) for shim-presence markers; the on-disk ``tests/`` tree for the
# migration-drain trigger (test files are not package resources).
_PKG = Path(str(_resource_files("hpc_agent")))
_TESTS = Path(__file__).resolve().parents[1]


# ── expiry-trigger primitives ──────────────────────────────────────────────
# Each returns ``(expired, detail)``. ``expired`` True means the shim has
# OUTLIVED its window and must now be deleted.


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse ``0.11.0`` / ``0.11.0+local`` → ``(0, 11, 0)`` (test_backcompat_expiry idiom)."""
    head = v.split("+", 1)[0].split("-", 1)[0]
    return tuple(int(p) for p in head.split(".") if p.isdigit())


def _version_reached(remove_at: str) -> Callable[[], tuple[bool, str]]:
    """Expired once the package version reaches ``remove_at`` — the "for one
    release" / "removed in a future release" promise made mechanical."""

    def check() -> tuple[bool, str]:
        current = _version_tuple(__version__)
        target = _version_tuple(remove_at)
        return current >= target, (
            f"package version {__version__} has reached the {remove_at} removal target"
            if current >= target
            else f"package version {__version__} < removal target {remove_at}"
        )

    return check


def _calendar_reached(sunset: date, *, note: str) -> Callable[[], tuple[bool, str]]:
    """Expired once the calendar passes ``sunset`` — for windows keyed to
    wall-clock time rather than a release (e.g. "no mid-flight run predates
    the fix", which drains as old runs finish, not as versions ship)."""

    def check() -> tuple[bool, str]:
        today = date.today()
        expired = today >= sunset
        return expired, (
            f"sunset {sunset.isoformat()} passed ({note})"
            if expired
            else f"today {today.isoformat()} < sunset {sunset.isoformat()} ({note})"
        )

    return check


def _migration_drained(usage_marker: str) -> Callable[[], tuple[bool, str]]:
    """Expired once NO file under ``tests/`` still references ``usage_marker`` —
    the honest end-condition for a shim kept alive purely to serve legacy call
    sites ("kept for ~20 legacy test files"). Removing the last caller frees
    the shim, so the row must flip red to demand the shim's deletion."""

    def check() -> tuple[bool, str]:
        callers = sorted(
            p.relative_to(_TESTS).as_posix()
            for p in _TESTS.rglob("*.py")
            if p.name != Path(__file__).name and usage_marker in p.read_text(encoding="utf-8")
        )
        n = len(callers)
        return n == 0, (
            f"no test file references {usage_marker!r} anymore — shim has no callers left"
            if n == 0
            else f"{n} test file(s) still reference {usage_marker!r}"
        )

    return check


# ── the ledger ─────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Shim:
    """One time-boxed compat shim and the condition that ends its window."""

    name: str
    path: Path  # source file that must still contain ``marker``
    marker: str  # stable substring proving the shim is still present
    reason: str  # the removal instruction, quoted from the shim's own note
    expired: Callable[[], tuple[bool, str]]  # () -> (outlived, detail)


LEDGER: list[Shim] = [
    Shim(
        name="legacy_terminal_block_keys",
        path=_PKG / "state" / "block_terminal.py",
        marker="def legacy_terminal_block_keys(",
        reason=(
            "Reader fallback to pre-2026-07-07 short terminal-block keys "
            '("s2"/"s3"/"s4"). Remove once no mid-flight run predates the '
            "canonical-key fix — old runs finish over wall-clock time, not releases."
        ),
        # HPC runs live days–weeks; a run started before the 2026-07-07 fix is
        # long finished by early October. Calendar-, not version-, gated.
        expired=_calendar_reached(date(2026, 10, 7), note="~3 months after the 2026-07-07 fix"),
    ),
    Shim(
        name="_MOVED root-namespace shim",
        path=_PKG / "__init__.py",
        marker="_MOVED: dict[str, str] = {",
        reason=(
            "Item-6 root-namespace re-exports: names that left ``hpc_agent`` still "
            "resolve via ``__getattr__`` with a DeprecationWarning. Docstring: works "
            '"for one release"; drop the shim (and its ``ALLOWED_EXPORTS`` allowlist) next minor.'
        ),
        expired=_version_reached("0.12.0"),
    ),
    Shim(
        name="get_template_path deprecated shim",
        path=_PKG / "__init__.py",
        marker="def get_template_path(",
        reason=(
            "Retained back-compat shim that materialises a rendered array script to "
            "disk; callers should use ``get_backend_class(scheduler).render_script(...)``. "
            "Drop next minor."
        ),
        expired=_version_reached("0.12.0"),
    ),
    Shim(
        name="HPC_HOMEDIR monkeypatch back-compat",
        path=_PKG / "state" / "run_record.py",
        marker="HPC_HOMEDIR = (",
        reason=(
            "Module-level ``HPC_HOMEDIR`` attribute kept so the pre-v3 "
            '``monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path)`` pattern in legacy '
            "test files keeps redirecting state writes. Remove once every test has moved "
            'to ``monkeypatch.setenv("HPC_JOURNAL_DIR", ...)``.'
        ),
        # Migration-gated: the shim exists only for its test callers. When the
        # last one is ported (setenv), the shim is dead weight → flip red.
        expired=_migration_drained("HPC_HOMEDIR"),
    ),
    Shim(
        name="state.run_record.runs_dir forwarder",
        path=_PKG / "state" / "run_record.py",
        marker="def runs_dir(",
        reason=(
            "Deprecated forwarder for ``JournalLayout(experiment_dir).runs``. "
            "Callers should use ``JournalLayout`` directly. Drop next minor."
        ),
        expired=_version_reached("0.12.0"),
    ),
    Shim(
        name="state.run_record._run_path forwarder",
        path=_PKG / "state" / "run_record.py",
        marker="def _run_path(",
        reason=(
            "Deprecated alias for ``JournalLayout(experiment_dir).run_record(run_id)``. "
            "Drop next minor."
        ),
        expired=_version_reached("0.12.0"),
    ),
    Shim(
        name="state.run_record._atomic_write_json forwarder",
        path=_PKG / "state" / "run_record.py",
        marker="def _atomic_write_json(",
        reason=(
            "Deprecated forwarder for ``hpc_agent.infra.io.atomic_write_json``; the "
            'docstring says it "will be removed in a future release." Drop next minor.'
        ),
        expired=_version_reached("0.12.0"),
    ),
]


# ── the punch-list test ────────────────────────────────────────────────────


@pytest.mark.parametrize("shim", LEDGER, ids=[s.name for s in LEDGER])
def test_compat_shim_within_expiry_window(shim: Shim) -> None:
    """Each time-boxed shim ``xfails`` while in-window and ``fails`` once outlived.

    Two-part enforcement, mirroring ``test_recovery_registry.py``:

    1. *Presence guard.* The shim's ``marker`` must still be in its source
       file. If it's gone (shim deleted or renamed), this ``fails`` — a stale
       ledger row must be dropped, exactly like a ported kind that lingers in
       the un-ported parametrize list.
    2. *Expiry gate.* If the shim's mechanical expiry condition has fired, this
       ``fails`` loudly with the removal instruction (strict-xpass). Otherwise
       it ``xfails`` — the steady state while the shim is legitimately alive.
    """
    source = shim.path.read_text(encoding="utf-8")
    assert shim.marker in source, (
        f"{shim.name}: marker {shim.marker!r} not found in "
        f"{shim.path.name} — the shim was removed or renamed. Drop this ledger row."
    )

    outlived, detail = shim.expired()
    if outlived:
        # Strict-xpass: the window has closed but the shim is still here.
        pytest.fail(
            f"{shim.name}: compat shim has OUTLIVED its deprecation window "
            f"({detail}). Removal condition: {shim.reason} "
            f"Delete the shim in {shim.path.name} AND remove this ledger row."
        )
    pytest.xfail(f"{shim.name}: still within its window — {detail}")


def test_ledger_markers_are_unique_per_source_file() -> None:
    """No two rows may share a (file, marker) pair — a duplicated marker would
    let one shim's removal silently satisfy another row's presence guard."""
    seen: set[tuple[str, str]] = set()
    dupes: list[str] = []
    for shim in LEDGER:
        key = (shim.path.as_posix(), shim.marker)
        if key in seen:
            dupes.append(f"{shim.name}: duplicate marker {shim.marker!r} in {shim.path.name}")
        seen.add(key)
    assert not dupes, "Ambiguous ledger markers:\n  " + "\n  ".join(dupes)
