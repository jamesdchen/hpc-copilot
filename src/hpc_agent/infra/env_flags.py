"""Boolean env-var flags, parsed one way everywhere.

The single truthiness convention for operator flags
(``HPC_AGENT_ALWAYS_CANARY``, the decode-schema gates, …): unset or
blank means *default*; any explicit value is parsed strictly — only
``1``/``true``/``yes``/``on`` enable, everything else (``0``,
``false``, …) disables, so a documented off-switch works on a
default-on flag. Extracted so the gates cannot drift apart
(``_decode_schema_enabled`` and ``_always_canary`` previously each
inlined this parse).
"""

from __future__ import annotations

import os

__all__ = ["env_flag"]

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def env_flag(var: str, *, default: bool = False) -> bool:
    """Whether the boolean env flag *var* is on (unset/blank → *default*)."""
    value = os.environ.get(var, "").strip()
    if not value:
        return default
    return value.lower() in _TRUTHY
