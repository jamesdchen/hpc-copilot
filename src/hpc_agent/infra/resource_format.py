# @pure: no-io
"""Canonical resource value-coercion for the submit-time render layer.

Before this module existed, the three things you do to a resource value
just before it lands in a scheduler directive — *clamp* it to the
cluster's limits, *round it up* so you never ask for a fractional
core/node, and *format* an integer walltime as ``HH:MM:SS`` — were
hand-rolled at every call site. Walltime→``HH:MM:SS`` in particular had
two independent implementations (``infra/backends/sge.py::_fmt_hms`` and
``ops/recover_flow.py::_format_walltime``) that happened to agree for
non-negative inputs but disagreed on negatives, and nothing guaranteed a
third copy wouldn't drift further. The throughput planner clamped to
``max_array_size`` inline; ``infra/clusters.py`` clamped walltime to the
cluster ceiling inline; each was individually correct but un-auditable as
a *policy* because the policy lived in N places.

This is the hpc-agent port of remotemanager's ``Substitution._format_value``
(reference only — we did **not** take the dependency, nor its ``DelayVar``
deferred-arithmetic machinery, nor its YAML-Computer jobscript model). We
keep our ``{{TOKEN}}`` template syntax untouched and lift only the
*coercion semantics* into two small functions:

* :func:`walltime_hms` — the ONE canonical integer-seconds → ``HH:MM:SS``
  formatter. Every scheduler path that needs that string calls this, so
  the format has exactly one implementation.
* :func:`coerce` — the declarative "clamp + ceil + format" pipeline a
  render site applies to a single value: drop ``None`` (so an unset
  optional directive is omitted uniformly), ``math.ceil`` numerics,
  clamp into ``[minimum, maximum]``, then optionally format. ``fmt="time"``
  delegates to :func:`walltime_hms` so the two stay welded together.

Design constraints (enforced by CI):

* **stdlib-only** (``math``). This runs at submit-time on the orchestrator,
  but the ``# @pure: no-io`` header above is checked by
  ``scripts/lint_pure_files.py`` — keep it free of I/O so it can be
  imported by the planning helpers without dragging in side effects.
* **behaviour-preserving.** The edge cases below were chosen to reproduce
  byte-for-byte what the existing call sites (and their pinning tests)
  already emit; this module is a consolidation, not a behaviour change.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["coerce", "walltime_hms"]


def walltime_hms(seconds: int) -> str:
    """Format integer *seconds* as a scheduler ``HH:MM:SS`` walltime string.

    This is the single canonical second→``HH:MM:SS`` formatter for the
    whole codebase; SLURM ``--time=HH:MM:SS``, SGE ``-l h_rt=HH:MM:SS``
    and any future scheduler all route through here so the rendering can
    never drift between code paths.

    Edge-case behaviour (chosen to match the two implementations this
    replaces, so existing pinning tests pass unchanged):

    * **Zero** → ``"00:00:00"`` — minutes and seconds are always
      two-digit, hours are two-digit *minimum*.
    * **>= 100h** → hours overflow past two digits rather than wrap, e.g.
      ``360000`` → ``"100:00:00"``. Schedulers accept an un-capped hours
      field (a multi-day ``h_rt``/``--time`` is legal), and the old
      ``sge._fmt_hms`` documented exactly this ("hours are not zero-padded
      to two digits (SGE accepts >99h)"). The ``%02d`` here is a
      *minimum* width, so it pads small values and leaves large ones
      intact — identical to the replaced code (e.g. ``90061`` →
      ``"25:01:01"``).
    * **Negative** → clamped to ``0`` (``"00:00:00"``). A negative
      walltime is nonsensical and the scheduler would reject the literal
      ``-1`` in the directive; clamping mirrors the old
      ``sge._fmt_hms``'s ``max(0, ...)`` guard. (The recover-flow copy
      lacked this guard, but every one of its call sites is already
      behind a ``walltime_sec > 0`` check, so no current output changes.)
    * **Non-int numerics** (``float``/numeric ``str``) are coerced through
      ``int()`` — a truncating cast, matching the historical
      ``int(total_seconds)`` the SGE path applied. Use :func:`coerce`
      with ``ceil=True`` upstream if you need round-*up* semantics before
      formatting.
    """
    # ``int(...)`` truncates floats toward zero and parses an all-digit
    # string, reproducing the old ``int(total_seconds)`` coercion; the
    # ``max(0, ...)`` then collapses any negative (or negative-truncating)
    # value to the zero walltime instead of emitting a ``-`` in the field.
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    # ``{:02d}`` is a *minimum* field width: it left-pads values < 10 to
    # two digits and leaves >= 100h untouched, which is exactly the
    # "render >99h correctly" guarantee both predecessors relied on.
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# Registry of named ``fmt`` codes. ``"time"`` is the only one today; it
# mirrors remotemanager's ``format="time"`` and is kept as a table so a
# future ``fmt="mem"`` etc. is a one-line addition rather than another
# ``if`` branch sprinkled across coerce().
_FORMATTERS: dict[str, Callable[[int], str]] = {
    "time": walltime_hms,
}


# Typed overloads so call sites get a precise return type rather than the
# union ``object | None``: a ``None`` value stays ``None``; a ``fmt`` string
# yields a formatted ``str``; an unformatted numeric value yields a number
# (``int`` when ``ceil``/an integer bound coerced it, else the input float).
# The runtime body is the single implementation below.
@overload
def coerce(
    value: None,
    *,
    minimum: float | None = ...,
    maximum: float | None = ...,
    ceil: bool = ...,
    fmt: str | None = ...,
) -> None: ...


@overload
def coerce(
    value: float,
    *,
    minimum: float | None = ...,
    maximum: float | None = ...,
    ceil: bool = ...,
    fmt: str,
) -> str: ...


@overload
def coerce(
    value: float,
    *,
    minimum: float | None = ...,
    maximum: float | None = ...,
    ceil: bool = ...,
    fmt: None = ...,
) -> int | float: ...


def coerce(
    value: float | None,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    ceil: bool = False,
    fmt: str | None = None,
) -> object | None:
    """Apply the canonical clamp → ceil → format pipeline to one value.

    This is the declarative coercion a render site runs on a single
    resource value just before it becomes a scheduler directive. The
    steps run in a fixed, documented order:

    1. **None passthrough.** ``value is None`` returns ``None`` untouched,
       so the caller can drop the whole directive line for an unset
       optional ask — the uniform "omit optional directive" behaviour that
       was previously open-coded as ``if x is not None`` guards at each
       site (and as ``Substitution``'s "drop the line when value is None"
       in the reference implementation).
    2. **Ceil.** With ``ceil=True`` the value is rounded *up* via
       ``math.ceil`` before any clamping — you can't request 1.2 cores or
       0.5 nodes, so a fractional ask becomes the next whole unit. (Ceil
       precedes clamp so a value rounding up to exactly ``maximum`` is
       kept, not pushed over the ceiling and then clamped back — the two
       orders agree at the boundary, but ceil-first is the intuitive one.)
    3. **Clamp.** ``maximum`` caps the value (``min``) and ``minimum``
       floors it (``max``). ``maximum`` is applied first so that when a
       caller passes a ``minimum`` *above* a ``maximum`` (a contradictory
       limit pair) the floor wins and the result is at least ``minimum``;
       this is the same precedence the inline ``min(ask, ceiling)`` /
       ``max(1, n)`` sites had when composed. Either bound may be ``None``
       to leave that side unconstrained.
    4. **Format.** With ``fmt`` set, the (now integer-domain) value is
       passed to the matching formatter and a ``str`` is returned.
       ``fmt="time"`` delegates to :func:`walltime_hms`. Formatting forces
       the value through ``int()`` first because every formatter today
       wants whole units; an unknown ``fmt`` raises :class:`ValueError`
       rather than silently returning the number unformatted.

    Returns the coerced value: a number when ``fmt is None`` (an ``int``
    if ``ceil`` rounded it or a bound coerced it, otherwise the original
    numeric type), a ``str`` when ``fmt`` is set, or ``None`` for a
    ``None`` input.
    """
    if value is None:
        # Optional-directive omission: the caller decides what "no value"
        # means (skip the line); we just refuse to invent a default.
        return None

    out: float = value
    if ceil:
        # math.ceil returns an int; this is the "no fractional
        # cores/nodes" guarantee. Done before clamping (see step 2).
        out = math.ceil(out)
    if maximum is not None:
        out = min(out, maximum)
    if minimum is not None:
        out = max(out, minimum)

    if fmt is not None:
        formatter = _FORMATTERS.get(fmt)
        if formatter is None:
            raise ValueError(f"coerce: unknown fmt {fmt!r}; known formats: {sorted(_FORMATTERS)}")
        # Formatters operate on whole units; ``int(out)`` truncates any
        # residual float (there is none once ``ceil`` ran, but a caller
        # may format without ceiling). walltime_hms re-applies its own
        # ``max(0, int(...))`` so this stays correct for negatives too.
        return formatter(int(out))

    return out
