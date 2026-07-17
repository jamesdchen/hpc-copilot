"""GOLDEN byte-identity pin for the harness-activation profile refactor (Wave 2).

The activation profile (:data:`hpc_agent.agent_assets.CLAUDE_CODE_PROFILE`) and
its renderer (:class:`hpc_agent.harness_profile.ClaudeCodeProfile`) lift the
declarative content of ``install_agent_assets`` behind a frozen profile. This
module proves that refactor is INERT: the rendered ``settings.json`` /
``.claude.json`` is byte-for-byte the pre-refactor install output.

**The golden is a GOLDEN-OF-A-PURE-FUNCTION over PINNED HERMETIC INPUTS
(premortem D5), never captured from a live install.** ``install_agent_assets``'s
output is non-deterministic on three sources — the absolute ``sys.executable``
embedded in every hook + MCP command, the git-sha wheel ``__version__``, and the
ambient clusters config that shapes the deny rules — so a naive
"captured-from-real-output" golden would be flaky and non-portable across the
Windows/Linux CI legs. Instead we drive :func:`_install_from_profile` with a
FIXED fake interpreter, a fixed cluster host, and a FIXTURE asset tree, and pin
the exact rendered bytes. The fixture ``tests/cli/data/profile_install_golden.json``
was captured from the PRE-refactor body under these same hermetic inputs; a
change to any profile FIELD (a needle, event, matcher, pre-filter verb, the MCP
argv, the stop guards) changes the rendered bytes and breaks this test. Per D5
the golden is scoped to the ``settings.json`` / ``.claude.json`` render and
EXCLUDES the ``__version__``-stamped manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import CLAUDE_CODE_PROFILE, _install_from_profile
from hpc_agent.harness_profile import ClaudeCodeProfile

# The pinned hermetic inputs — MUST match the values the golden fixture was
# captured under. A forward-slash, space-free interpreter path renders
# identically on Windows and POSIX (``_hook_python`` only rewrites backslashes /
# quotes spaces), so the golden is cross-OS portable.
_HERMETIC_PY = "/hermetic/py/python"
_HERMETIC_HOST = "cluster.example.test"

_GOLDEN_PATH = Path(__file__).resolve().parent / "data" / "profile_install_golden.json"


def _fixture_tree(root: Path) -> None:
    """A tiny asset tree pinning the installed skill names (→ ``permissions.allow``)."""
    cmds = root / "commands"
    cmds.mkdir(parents=True)
    (cmds / "zzz-demo.md").write_text("# demo command\n", encoding="utf-8")
    skills = root / "skills"
    for name in ("alpha", "beta"):
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture skill {name}\n---\nbody\n",
            encoding="utf-8",
        )


def _render_hermetic(tmp_path: Path) -> tuple[str, str]:
    """Install under the pinned hermetic inputs; return (settings.json, .claude.json) text."""
    fixture = tmp_path / "assets"
    _fixture_tree(fixture)
    claude_dir = tmp_path / "claude"
    _install_from_profile(
        CLAUDE_CODE_PROFILE,
        claude_dir=claude_dir,
        dry_run=False,
        executable=_HERMETIC_PY,
        version="0.0.0-golden",
        cluster_hosts=(_HERMETIC_HOST,),
        asset_roots=[fixture],
    )
    settings = (claude_dir / "settings.json").read_text(encoding="utf-8")
    mcp = (claude_dir.parent / ".claude.json").read_text(encoding="utf-8")
    return settings, mcp


def test_settings_json_render_is_byte_identical_to_golden(tmp_path: Path) -> None:
    """``ClaudeCodeProfile`` renders the pre-refactor ``settings.json`` byte-for-byte."""
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    settings, _mcp = _render_hermetic(tmp_path)
    assert settings == golden["settings_json"]


def test_claude_json_mcp_render_is_byte_identical_to_golden(tmp_path: Path) -> None:
    """The MCP ``.claude.json`` render is byte-for-byte the pre-refactor output."""
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    _settings, mcp = _render_hermetic(tmp_path)
    assert mcp == golden["claude_json"]


def test_every_rendered_hook_command_embeds_its_needle(tmp_path: Path) -> None:
    """The needle-embed obligation (activation plan §5-R2 / premortem D8) for OUR
    renderer: every rendered hook command MUST contain the descriptor's needle
    substring, or the capability probe / re-find silently orphans the hook."""
    for descriptor in CLAUDE_CODE_PROFILE.hook_descriptors:
        command = ClaudeCodeProfile.hook_command(descriptor, _HERMETIC_PY)
        assert descriptor.needle in command, (
            f"hook command for {descriptor.needle} dropped its needle: {command!r}"
        )

    # The fused Stop command must embed the multiplex needle AND every guard needle.
    stop = CLAUDE_CODE_PROFILE.stop_hook
    stop_command = ClaudeCodeProfile.stop_command(stop, _HERMETIC_PY)
    assert stop.needle in stop_command
    for guard in stop.guards:
        assert guard in stop_command, f"stop command dropped guard needle {guard}: {stop_command!r}"


def test_a_profile_field_change_breaks_the_golden(tmp_path: Path) -> None:
    """The golden is sensitive to the PROFILE — mutating a descriptor's pre-filter
    changes the rendered bytes, so the golden would go red (the pin's whole point).

    Uses a locally-constructed variant profile (the module singleton is frozen and
    never mutated) to demonstrate the sensitivity without touching global state."""
    import dataclasses

    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    original = CLAUDE_CODE_PROFILE.hook_descriptors[0]
    drifted = dataclasses.replace(original, prefilter=("emit-skill-return", "extra-verb"))
    variant = dataclasses.replace(
        CLAUDE_CODE_PROFILE,
        hook_descriptors=(drifted, *CLAUDE_CODE_PROFILE.hook_descriptors[1:]),
    )
    fixture = tmp_path / "assets"
    _fixture_tree(fixture)
    claude_dir = tmp_path / "claude"
    _install_from_profile(
        variant,
        claude_dir=claude_dir,
        dry_run=False,
        executable=_HERMETIC_PY,
        version="0.0.0-golden",
        cluster_hosts=(_HERMETIC_HOST,),
        asset_roots=[fixture],
    )
    settings = (claude_dir / "settings.json").read_text(encoding="utf-8")
    assert settings != golden["settings_json"], "a profile field change must break the golden"
