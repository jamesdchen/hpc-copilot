"""Shared Pydantic types reused across multiple wire schemas.

These mirror the named ``$defs`` inside ``schemas/envelope.json``
(``run_id``, ``run_id_strict``, etc.). Today the envelope.json file
is still hand-authored for shared definitions; once every consumer
schema is Pydantic-emitted, envelope.json's ``$defs`` block can
itself be regenerated from these aliases (or deleted in favor of
inlining, since each generated schema is self-contained).

Aliases are deliberately ``Annotated`` rather than custom ``BaseModel``
subclasses so they inline as ``{type: "string", pattern: "..."}`` in
the emitted schema without introducing a per-model ``$defs`` entry —
keeps the diff against the hand-authored JSON minimal.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

# Filesystem-safe run-identifier shape. Loose form (output): any
# string. Strict form (input): alphanumerics + dot + underscore +
# hyphen. Mirrors envelope.json#/$defs/run_id_strict.
RunIdStrict = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]

# SSH target: ``user@host`` (or OpenSSH alias resolving to the same).
# Mirrors the inline pattern in submit_flow.input.json today.
SshTarget = Annotated[str, StringConstraints(pattern=r"^[^@]+@[^@]+$")]

# Campaign identifier. Same character class as RunIdStrict but
# semantically distinct — keep the alias separate so a future
# tightening of one doesn't silently change the other.
CampaignId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._\-]+$")]
