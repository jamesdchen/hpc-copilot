"""Red-team pins for the ONE §3 containment guard (``sandbox_guard.py``).

Defect 1 (red-teamed 2026-07-18): the pre-fix guards compared
``Path(env).expanduser().resolve()`` by normcased string, and on Windows 11 /
Python 3.13 that resolve does NOT canonicalize the deliberate alias spellings
of the SAME directory — ``\\?\\C:\\...``, ``\\?\\UNC\\localhost\\C$\\...``,
``\\\\localhost\\C$\\...``, ``\\\\127.0.0.1\\C$\\...`` all PASSED while naming
the production journal home at the OS level (``os.path.samefile`` proves it).

These tests pin the fix end to end, with no docker, no SSH, and no cluster:

* the ALIAS corpus — every deliberate spelling of ``~/.claude/hpc`` (or a
  subdir of it) MUST be judged within the production home: at the helper
  level AND through BOTH public guards (``sandbox_fixture`` +
  ``sandbox_seed``), with ``os.path.samefile`` premise proofs that the
  spellings really are the production home at the OS level;
* the ACCIDENTAL corpus — the pre-fix refusals (case, separators, dot
  segments, trailing space/dot) MUST stay refused;
* the ALLOWED side — ephemeral sandbox homes (including THEIR OWN alias
  spellings) and siblings of the production home MUST pass, so the fix can
  never over-block a legitimate sandbox;
* the IDENTITY pin — every guard consumer binds the SAME guard module
  object, so no consumer can silently drift back to a local copy;
* POSIX passthrough — the alias layer is inert off Windows (no false
  positives): Windows spellings stay ordinary (weird) relative paths and the
  guard reduces to resolve + normcase + prefix + samefile.

Windows-only assertions skip on POSIX and vice versa via ``windows_only`` /
``posix_only``; share-level OS proofs additionally skip when the box does not
serve the admin share (the string-layer refusals need no share at all).
"""

from __future__ import annotations

import contextlib
import os
import socket
from pathlib import Path

import pytest
import sandbox_fixture
import sandbox_guard
import sandbox_seed
from sandbox_fixture import SandboxTrustError, require_sandbox_journal_home
from sandbox_guard import (
    admin_share_reachable,
    canonical_journal_path,
    is_within_production_home,
    production_accidental_spellings,
    production_alias_spellings,
    production_journal_home,
)
from sandbox_seed import SandboxSeedError, assert_sandbox_journal_home

ON_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows path semantics")
posix_only = pytest.mark.skipif(ON_WINDOWS, reason="POSIX path semantics")


def _drive_share(path: Path) -> tuple[str, str] | None:
    """(share, rest) for a local-drive path: ('C$', 'Users\\\\...'); None else."""
    drive = path.drive
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        return f"{drive[0]}$", str(path)[len(drive) :].lstrip("\\/")
    return None


def _build_alias_cases() -> tuple[dict[str, str], dict[str, str]]:
    """(refusal corpus, samefile-premise corpus) of production-home spellings.

    The refusal corpus is the guard's own :func:`production_alias_spellings`
    plus supplementary hostile variants (mixed case, forward slashes,
    trailing-dot host, the loopback/hostname ext-UNC forms). The premise
    corpus is the subset naming the production home ITSELF (never a subdir)
    for the ``os.path.samefile`` proofs.
    """
    cases = production_alias_spellings()
    premise: dict[str, str] = {}
    if not ON_WINDOWS:
        return cases, premise
    prod = production_journal_home()
    share_rest = _drive_share(prod)
    if share_rest is None:
        return cases, premise
    share, rest = share_rest
    fwd_rest = rest.replace("\\", "/")
    hostname = ""
    with contextlib.suppress(OSError):
        hostname = socket.gethostname()
    lowercase_drive = "\\\\?\\" + str(prod.drive).lower() + str(prod)[len(prod.drive) :]
    supplement = {
        "ext_prefix_fwd": "\\\\?\\" + str(prod).replace("\\", "/"),
        "ext_prefix_lowercase_drive": lowercase_drive,
        "ext_prefix_upper": "\\\\?\\" + str(prod).upper(),
        "share/ext_unc_loopback": f"\\\\?\\UNC\\127.0.0.1\\{share}\\{rest}",
        "share/localhost_fwd": f"//localhost/{share}/{fwd_rest}",
        "share/localhost_trailing_dot": f"\\\\localhost.\\{share}\\{rest}",
        "share/localhost_subdir": f"\\\\localhost\\{share}\\{rest}\\newsub",
        "share/upper_host_low_drive": f"\\\\LOCALHOST\\{share.lower()}\\{rest}",
    }
    if hostname:
        supplement["share/hostname_upper"] = f"\\\\{hostname.upper()}\\{share}\\{rest}"
        supplement["share/ext_unc_hostname"] = f"\\\\?\\UNC\\{hostname}\\{share}\\{rest}"
    cases.update(supplement)
    for case, spelling in cases.items():
        # Premise = spellings the OS genuinely resolves to the home ITSELF.
        # Excluded: subdir spellings (the home itself is not named) and
        # ext_prefix_fwd — ``\\?\`` + forward slashes is NOT a valid Win32
        # path (the extended-length prefix demands backslashes), so the OS
        # never opens it; it stays in the refusal corpus as defense-in-depth.
        if not case.endswith("subdir") and case != "subdir" and case != "ext_prefix_fwd":
            premise[case] = spelling
    return cases, premise


_ALIAS_CASES, _PREMISE_CASES = _build_alias_cases()
_ACCIDENTAL_CASES = production_accidental_spellings()


# ── the alias corpus: every deliberate spelling MUST be blocked ──────────────


@pytest.mark.parametrize("case", sorted(_ALIAS_CASES))
def test_alias_spelling_of_production_home_is_blocked(case: str) -> None:
    spelling = _ALIAS_CASES[case]
    assert is_within_production_home(spelling), f"{case}: {spelling!r} not judged within"
    # The canonical form is the plain production home (or a subdir of it) —
    # the de-aliasing itself is what the string leg stands on.
    canonical = canonical_journal_path(spelling)
    prod = canonical_journal_path(production_journal_home())
    assert canonical == prod or prod in canonical.parents, f"{case}: {canonical}"


@pytest.mark.parametrize("case", sorted(_ALIAS_CASES))
def test_fixture_guard_refuses_alias_spelling(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", _ALIAS_CASES[case])
    with pytest.raises(SandboxTrustError, match="production journal home"):
        require_sandbox_journal_home()


@pytest.mark.parametrize("case", sorted(_ALIAS_CASES))
def test_seed_guard_refuses_alias_spelling(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", _ALIAS_CASES[case])
    with pytest.raises(SandboxSeedError, match="production journal home"):
        assert_sandbox_journal_home(_ALIAS_CASES[case])


@pytest.mark.parametrize("case", sorted(_PREMISE_CASES))
def test_alias_spelling_is_production_home_at_os_level(case: str) -> None:
    """The red-team premise: the spelling REALLY IS the production home.

    Without this, a corpus entry could be vacuous (a spelling the OS would
    never resolve to the home). samefile is alias-proof: it compares volume +
    file-index, so a pass here proves the spelling names the same inode —
    and the refusal tests above prove the guard blocks it anyway.
    """
    if not ON_WINDOWS:
        pytest.skip("Windows alias spellings")
    prod = production_journal_home()
    if not prod.exists():
        pytest.skip("no production journal home on this box")
    spelling = _PREMISE_CASES[case]
    if case.startswith("share/") and not admin_share_reachable(spelling):
        pytest.skip("this box does not serve the admin share")
    assert os.path.samefile(spelling, prod), f"{case}: {spelling!r} is not {prod}"


# ── the accidental corpus: the pre-fix refusals MUST stay refused ────────────


@pytest.mark.parametrize("case", sorted(_ACCIDENTAL_CASES))
def test_accidental_spelling_stays_blocked(case: str) -> None:
    spelling = _ACCIDENTAL_CASES[case]
    assert is_within_production_home(spelling), f"{case}: {spelling!r} not judged within"


# ── the ALLOWED side: no over-blocking of a legitimate sandbox ───────────────


def _allowed_spellings(base: Path) -> dict[str, str]:
    """Plain + (Windows) alias spellings of a legitimate NON-production path."""
    cases = {"plain": str(base)}
    if ON_WINDOWS:
        cases["ext_prefix"] = "\\\\?\\" + str(base)
        share_rest = _drive_share(base)
        if share_rest is not None:
            share, rest = share_rest
            cases["share/localhost"] = f"\\\\localhost\\{share}\\{rest}"
            cases["share/ext_unc_loopback"] = f"\\\\?\\UNC\\127.0.0.1\\{share}\\{rest}"
    return cases


def test_legitimate_sandbox_paths_are_allowed(tmp_path: Path) -> None:
    prod = production_journal_home()
    targets = {
        "ephemeral": tmp_path / "sandbox_journal_home",
        "sibling_of_production": prod.parent / "hpc-sandbox",
        "deep_nonexistent": tmp_path / "a" / "b" / "c",
    }
    for name, target in sorted(targets.items()):
        for case, spelling in sorted(_allowed_spellings(target).items()):
            assert not is_within_production_home(spelling), f"{name}/{case}: {spelling!r}"


@windows_only
def test_alias_spellings_of_a_sandbox_home_agree_and_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An aliased env + its plain declaration name ONE home: pass, agree.

    The agreement leg must treat two spellings of the same sandbox home as
    the same target (never a divergence), and both public guards must accept
    the home and return the canonical (de-aliased) form.
    """
    plain = str(tmp_path / "sandbox_journal_home")
    canonical = canonical_journal_path(plain)
    for case, spelling in sorted(_allowed_spellings(Path(plain)).items()):
        monkeypatch.setenv("HPC_JOURNAL_DIR", spelling)
        assert require_sandbox_journal_home() == canonical, case
        assert assert_sandbox_journal_home(plain) == canonical, case
        assert assert_sandbox_journal_home(spelling) == canonical, case


# ── the identity pin: every consumer binds the ONE guard module object ───────


def test_one_guard_module_object() -> None:
    """Import identity: no consumer can silently drift back to a local copy."""
    assert sandbox_fixture._GUARD is sandbox_guard
    assert sandbox_seed._GUARD is sandbox_guard
    for consumer in (sandbox_fixture, sandbox_seed):
        assert consumer._GUARD.is_within_production_home is (
            sandbox_guard.is_within_production_home
        )
        assert consumer._GUARD.canonical_journal_path is (sandbox_guard.canonical_journal_path)
        assert consumer._GUARD.same_journal_target is sandbox_guard.same_journal_target


def test_public_guards_lockstep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both public guards refuse the SAME spellings and accept the SAME homes."""
    for _case, spelling in sorted({**_ALIAS_CASES, **_ACCIDENTAL_CASES}.items()):
        monkeypatch.setenv("HPC_JOURNAL_DIR", spelling)
        with pytest.raises(SandboxTrustError, match="production journal home"):
            require_sandbox_journal_home()
        with pytest.raises(SandboxSeedError, match="production journal home"):
            assert_sandbox_journal_home(spelling)
    allowed = {
        "ephemeral": tmp_path / "sandbox_journal_home",
        "sibling_of_production": production_journal_home().parent / "hpc-sandbox",
    }
    for name, target in sorted(allowed.items()):
        for case, spelling in sorted(_allowed_spellings(target).items()):
            monkeypatch.setenv("HPC_JOURNAL_DIR", spelling)
            canonical = canonical_journal_path(spelling)
            assert require_sandbox_journal_home() == canonical, f"{name}/{case}"
            assert assert_sandbox_journal_home(spelling) == canonical, f"{name}/{case}"


# ── POSIX passthrough: the alias layer is inert off Windows ──────────────────


@posix_only
def test_posix_guard_semantics(tmp_path: Path) -> None:
    prod = production_journal_home()
    assert is_within_production_home(str(prod))
    assert is_within_production_home(str(prod / "deadbeefns"))
    assert not is_within_production_home(tmp_path / "sandbox_journal_home")
    assert not is_within_production_home(prod.parent / "hpc-sandbox")


@posix_only
def test_posix_alias_spellings_are_inert_passthrough() -> None:
    """Windows alias spellings must NOT de-alias on POSIX (no false positive).

    Off Windows they are ordinary (weird) relative paths: they resolve under
    the cwd, never to ``~/.claude/hpc``, so the guard neither blocks them as
    production nor maps them onto a drive letter.
    """
    prod = production_journal_home()
    rest = str(prod).lstrip("/")
    for spelling in (
        "\\\\?\\" + str(prod),
        f"\\\\localhost\\C$\\{rest}",
        f"\\\\?\\UNC\\127.0.0.1\\C$\\{rest}",
    ):
        assert not is_within_production_home(spelling), spelling
