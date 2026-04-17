"""Helper module that is NOT an executor — no main guard, no CLI.

Present in the fixture to verify discovery filters it out.
"""

from __future__ import annotations


def note(msg: str) -> str:
    return f"[note] {msg}"
