"""The bundled ``clusters.yaml`` must ship credential fields as placeholders only.

``src/hpc_agent/config/clusters.yaml`` is package-data — it is shipped verbatim
inside the wheel. It is a TEMPLATE: the user-identifying credential fields
(``user`` / ``scratch`` / ``account`` / ``conda_envs``) must stay ``<...>``
placeholders so a developer who edits it locally with real creds (for testing)
cannot accidentally publish them in a release.

This is ``release_clusters_yaml_hazard`` made automatic: the leak was caught by
hand twice (creds stashed before each ``uv build``); this gate fails CI/pytest
the moment real creds land in the committed file. ``host`` (a public hostname)
and ``conda_source`` (a public cluster path) are exempt — they are not secrets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Fields that carry user-identifying credentials and must be placeholders.
_CRED_FIELDS = ("user", "scratch", "account")

_BUNDLED = Path(__file__).resolve().parents[2] / "src" / "hpc_agent" / "config" / "clusters.yaml"


def _is_placeholder(value: Any) -> bool:
    """True for an absent/null value or an angle-bracketed ``<your_*>`` token."""
    if value is None:
        return True
    s = str(value).strip()
    return s.startswith("<") and s.endswith(">")


def test_bundled_clusters_yaml_ships_only_placeholders() -> None:
    data = yaml.safe_load(_BUNDLED.read_text(encoding="utf-8")) or {}
    leaks: list[str] = []
    for cluster, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        for field in _CRED_FIELDS:
            if field in cfg and not _is_placeholder(cfg[field]):
                leaks.append(f"{cluster}.{field} = {cfg[field]!r}")
        for env in cfg.get("conda_envs") or []:
            if not _is_placeholder(env):
                leaks.append(f"{cluster}.conda_envs entry = {env!r}")
    assert not leaks, (
        "bundled clusters.yaml ships real credentials (every cred field must be a "
        f"<placeholder>): {'; '.join(leaks)}. This file is package-data shipped in "
        "the wheel — stash local creds before committing/building."
    )
