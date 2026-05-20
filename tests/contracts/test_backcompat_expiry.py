"""Back-compat forwarders carry a 'remove in X.Y.Z' deletion target.
This test trips when the package version reaches that target without
the forwarder being removed.

Each entry is (substring_to_grep, file_path, remove_in_version, hint).
The substring is a stable identifier of the forwarder (the name of the
re-exported symbol or a comment fingerprint). When the package version
is >= remove_in_version, the substring must be absent from the file.
"""

from __future__ import annotations

from importlib.resources import files as _resource_files

from hpc_agent import __version__

# (substring, relative_module, remove_in, hint)
_FORWARDERS: list[tuple[str, str, str, str]] = [
    (
        "_SKILL_NAMES",
        "agent_cli.py",
        "0.4.0",
        "drop the back-compat re-export; tests should import "
        "from hpc_agent.atoms.capabilities directly",
    ),
    (
        "_resolve_auto_retry",
        "agent_cli.py",
        "0.4.0",
        "drop the back-compat re-export; tests should import "
        "from hpc_agent.atoms.failures directly",
    ),
    (
        "HPC_SUBDIR",
        "__init__.py",
        "0.4.0",
        "drop the back-compat forwarder; callers should use RepoLayout(experiment_dir).hpc",
    ),
    (
        "data.errors",  # B3 legacy partial-errors shape
        "agent_cli.py",
        "0.4.0",
        "strip the legacy ``errors`` key from snap.to_dict() now that "
        "B3 partial_errors is the wire contract — pushed from 0.3.0 to "
        "0.4.0 because removing the field is itself a wire-shape "
        "breaking change and warrants its own tagged release",
    ),
]


def _version_tuple(v: str) -> tuple[int, ...]:
    head = v.split("+", 1)[0].split("-", 1)[0]
    return tuple(int(p) for p in head.split(".") if p.isdigit())


def test_backcompat_forwarders_removed_at_target_version() -> None:
    current = _version_tuple(__version__)
    overdue: list[str] = []
    for substring, rel_path, remove_in, hint in _FORWARDERS:
        target = _version_tuple(remove_in)
        if current < target:
            continue
        text = (_resource_files("hpc_agent") / rel_path).read_text()
        if substring in text:
            overdue.append(
                f"{rel_path}: substring {substring!r} should have been removed "
                f"at version {remove_in} (current: {__version__}). {hint}"
            )
    assert not overdue, "Back-compat forwarders past their expiry:\n  " + "\n  ".join(overdue)
