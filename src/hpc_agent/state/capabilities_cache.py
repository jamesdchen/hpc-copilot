"""Build+dist-keyed disk cache of the ``capabilities`` verb's output (R4 ruling).

``capabilities`` is typically an agent's FIRST call each session and is the most
expensive pure-query verb on a cold process: it always takes the full registry
walk (~100 modules), then projects the whole operations catalog
(:func:`operations_bootstrap`), builds the entire argparse tree
(``_live_subcommands`` → ``build_parser``), and scans every installed plugin's
manifest — MEASURED ~2.2 s cold. The bake cannot cover it in a plugin env (the
demo env carries ``hpc-agent-notebook-render``). This memoizes the resolved
output to disk so the second-and-later cold invocation in a build/env skips the
projection + render work:

    ~/.claude/hpc/capabilities_cache/g<BUILD_SHA>/<variant>.json

A hit is BYTE-IDENTICAL to the live walk: the cached payload is re-emitted
through the SAME envelope path (``_ok(..., name="capabilities")`` for the bare
variant; ``sys.stdout.write`` of the llms-full text for ``--full``), so the only
difference from a miss is that the catalog projection / render never runs.

**Two output-shape VARIANTS, keyed explicitly.** The handler produces two
distinct outputs and each is cached separately by filename:

* ``bare`` — ``hpc-agent capabilities``: the JSON envelope data payload (a dict).
* ``full`` — ``hpc-agent capabilities --full``: the plain-text llms-full dump (a
  str), which bypasses the envelope contract entirely.

``--full`` is the ONLY output-affecting flag on the verb (the CliShape declares
no other; the global ``--experiment-dir`` is not read by the handler). So there
is no un-keyed invocation to refuse.

**What invalidates a hit.**

* A NEW WHEEL — a new ``BUILD_SHA`` (even at the SAME package version, which this
  project force-reinstalls constantly) lands the cache in a fresh ``g<sha>`` dir,
  so a same-version reinstall never reuses a stale payload (the same
  content-keying discipline as ``describe_cache`` / ``baked_catalog_usable``).
* ANY CHANGE TO THE INSTALLED-DISTRIBUTION SET — a plugin install / uninstall /
  upgrade changes the catalog (new primitives, backends, subcommands, plugin
  manifests) AND the ``--full`` render. That is captured by the
  ``installed_dist_signature`` from :mod:`hpc_agent.cli._fast_path_cache`
  (imported LAZILY so this module stays leaf-cheap), stored inside the cache file
  and re-checked on load — a signature mismatch is a miss. Because a plugin
  changes the catalog WITHOUT necessarily changing ``BUILD_SHA``, the dist
  signature is the load-bearing invalidator in a plugin env.
* AN OUTPUT-AFFECTING ENV CHANGE — the bare envelope reports
  ``ssh_multiplexing`` from ``HPC_NO_SSH_MULTIPLEX``; that var is folded into the
  same stored signature so a hit can never serve the wrong flag. ``journal_dir``
  is the other env-derived field, but it is already partitioned by the cache path
  (derived from :func:`journal_homedir`), so a different journal home resolves to
  a different cache file by construction.

**What disables the cache outright** (``load`` → ``None``, ``store`` no-ops):

* ``BUILD_SHA is None`` — a source checkout / editable install / old wheel: no
  cheap content identity, so the cache is disabled by construction (a dev
  checkout is the pure pass-through path — output is byte-identical to no cache).
* ``BUILD_DIRTY`` — a wheel from a dirty tree: the sha names a commit the content
  diverged from, so ``g<sha>`` would not be content-true.
* An un-fingerprintable env (``installed_dist_signature`` raises) — degrade to
  the live walk rather than trust a degenerate key.

``HPC_NO_CAPABILITIES_CACHE=1`` additionally bypasses the cache. Everything is
opportunistic: any I/O or signature error falls through to the live path, never
raising.

**Store gate (premortem A1).** ``store`` refuses to persist unless the FULL
primitive registry walk has completed (``_REGISTRATION_DONE``). ``capabilities``
is not on the single-verb fast path (absent from ``VERB_MODULE_MAP``), so its
handler always runs post-``register_primitives`` and the latch is True in
practice — but the projection is only whole-truth against the full registry, so
the gate is asserted here defensively, exactly as ``describe_cache`` does. A
payload computed off a partial registry would poison every reader for the build's
lifetime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = ["cache_disabled", "load", "store"]

_DISABLE_ENV_VAR = "HPC_NO_CAPABILITIES_CACHE"

#: The two output shapes the handler emits — a hit must be keyed to the variant
#: that produced it (``capabilities`` vs ``capabilities --full`` differ).
_VARIANTS = frozenset({"bare", "full"})


def cache_disabled() -> bool:
    """True when ``HPC_NO_CAPABILITIES_CACHE=1`` opts the cache out."""
    return os.environ.get(_DISABLE_ENV_VAR) == "1"


def _full_registration_done() -> bool:
    """True only when the FULL primitive registry walk has completed.

    The ``capabilities`` payload projects the WHOLE operations catalog; it is
    stable only against the fully-walked registry. ``capabilities`` is never on
    the single-verb fast path, so this is True whenever the handler runs — but
    ``store`` still gates on it so a payload can never be persisted off a partial
    registry and poison full-path readers (premortem A1). Read as a module
    attribute (never ``from ... import _REGISTRATION_DONE``) to honour the
    module-private boundary.
    """
    from hpc_agent._kernel.registry import primitive

    return bool(getattr(primitive, "_REGISTRATION_DONE", False))


def _content_key() -> str | None:
    """Cache directory key for the running build, or ``None`` when disabled.

    ``g<BUILD_SHA>`` for a clean wheel (content-true — a new wheel gets a new sha
    even at the same version, so a same-version reinstall never reuses a stale
    dir). ``None`` for a source checkout (``BUILD_SHA is None``) or a dirty-tree
    wheel (``BUILD_DIRTY``), where the content is not cheaply identifiable and the
    cache is disabled by construction. Attributes are read off the module object
    (not ``from ... import BUILD_SHA``) so a test can flip them with
    ``monkeypatch.setattr``.
    """
    from hpc_agent import _build_info

    if _build_info.BUILD_SHA and not _build_info.BUILD_DIRTY:
        return f"g{_build_info.BUILD_SHA}"
    return None


def _output_signature() -> str | None:
    """Fingerprint of every non-build input that shapes the output, or ``None``.

    Combines the installed-distribution signature (plugins change the catalog,
    the backends, the subcommands, the manifests, and the ``--full`` render —
    without necessarily changing ``BUILD_SHA``) with the one output-affecting env
    var not already partitioned by the cache path (``HPC_NO_SSH_MULTIPLEX``, which
    drives the bare envelope's ``ssh_multiplexing`` flag). ``journal_dir`` needs
    no entry here — it is partitioned by the path via :func:`journal_homedir`.

    ``installed_dist_signature`` is imported LAZILY (keeping this a leaf module)
    and may raise on an un-fingerprintable env; that returns ``None`` so the
    caller degrades to the live walk rather than trust a degenerate key.
    """
    from hpc_agent.cli._fast_path_cache import installed_dist_signature

    try:
        dist = installed_dist_signature()
    except Exception:  # noqa: BLE001 — un-fingerprintable env → live walk
        return None
    ssh = os.environ.get("HPC_NO_SSH_MULTIPLEX", "")
    return f"{dist}|ssh={ssh}"


def _cache_path(variant: str) -> Path | None:
    """Cache file for *variant* under the build-key dir.

    ``None`` when the variant is unknown OR the cache is disabled for this build
    (source checkout / dirty wheel — see :func:`_content_key`). Resolving the
    journal home goes through the leaf :func:`journal_homedir`, which does not
    drag ``run_record``'s dataclasses + inspect chain onto every call.
    """
    if variant not in _VARIANTS:
        return None
    key = _content_key()
    if key is None:
        return None
    from hpc_agent.state._homedir import journal_homedir

    return journal_homedir() / "capabilities_cache" / key / f"{variant}.json"


def load(variant: str) -> dict[str, Any] | str | None:
    """Return the cached payload for *variant*, or ``None`` (any not-a-clean-hit).

    Returns a ``dict`` for the ``bare`` variant and a ``str`` for ``full`` — the
    shape the handler will re-emit through the same envelope path. ``None`` on
    cache-disabled (env or non-content-keyable build), a signature mismatch
    (new dist set / changed ``HPC_NO_SSH_MULTIPLEX``), an un-fingerprintable env,
    a miss, an unknown variant, a shape mismatch, or any read/parse error — every
    "not a clean hit" case collapses to "compute it live".
    """
    if cache_disabled():
        return None
    path = _cache_path(variant)
    if path is None:
        return None
    signature = _output_signature()
    if signature is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("signature") != signature:
        return None
    payload = data.get("payload")
    if variant == "bare" and isinstance(payload, dict):
        return payload
    if variant == "full" and isinstance(payload, str):
        return payload
    return None


def store(variant: str, payload: dict[str, Any] | str) -> None:
    """Cache the resolved *payload* for *variant* (best-effort, no-op if disabled).

    Refuses to persist under a PARTIAL registry (premortem A1): the payload is
    whole-truth only against the fully-walked registry. Also no-ops when the
    build is not content-keyable (source checkout / dirty wheel), the env is not
    fingerprintable, or the payload's shape does not match the variant. The
    signature is stored alongside the payload so a later dist/env change is
    detected on load.
    """
    if cache_disabled():
        return
    if not _full_registration_done():
        return
    if variant == "bare" and not isinstance(payload, dict):
        return
    if variant == "full" and not isinstance(payload, str):
        return
    path = _cache_path(variant)
    if path is None:
        return
    signature = _output_signature()
    if signature is None:
        return
    from hpc_agent.infra.io import atomic_write_json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"signature": signature, "payload": payload})
    except OSError:
        pass
