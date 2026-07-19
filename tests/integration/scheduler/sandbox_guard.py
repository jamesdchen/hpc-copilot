"""The ONE §3 trust-doctrine containment guard.

Plan ``docs/plans/sandbox-proving-run-2026-07-18.md`` §3: the sandbox must be
"structurally incapable of touching a production namespace". Both public
sandbox guards delegate here so the invariant can never drift into two
divergent copies:

* ``sandbox_fixture.require_sandbox_journal_home`` — thin wrapper stamping
  ``SandboxTrustError``;
* ``sandbox_seed.assert_sandbox_journal_home`` — thin wrapper stamping
  ``SandboxSeedError``, plus the env/declared agreement leg built on
  :func:`same_journal_target`.

# MIRROR: sandbox_fixture.py::require_sandbox_journal_home <->
#   sandbox_seed.py::assert_sandbox_journal_home
#   pinned-by tests/integration/scheduler/test_sandbox_guard.py::test_public_guards_lockstep

Defect 1 (red-teamed 2026-07-18) — why canonicalization comes FIRST: the
original guards compared ``Path(env).expanduser().resolve()`` by normcased
string against the production home. On Windows 11 / Python 3.13,
``resolve()`` does NOT map the deliberate alias spellings back to plain DOS
form, so all of these PASSED while naming the production home at the OS
level:

* ``\\?\\C:\\...`` (the extended-length prefix),
* ``\\?\\UNC\\localhost\\C$\\...``,
* ``\\\\localhost\\C$\\...`` / ``\\\\127.0.0.1\\C$\\...`` (admin shares).

``state.run_record.current_homedir`` returns the env value VERBATIM, so a
pass landed every journal write in the production namespace while the
sandbox believed itself isolated. The fix, in ONE place:

1. :func:`canonical_journal_path` strips the alias prefixes and maps local
   admin-share spellings (``\\\\{localhost,127.0.0.1,<hostname>}\\<drive>$``)
   to drive letters BEFORE resolving — and strips AGAIN after, because
   ``resolve()`` itself may return an aliased form;
2. the containment test runs the original normcase + prefix comparison on
   that canonical form, so every accidental-spelling refusal (case,
   separator, dot segments, trailing space, reparse point, relative) is
   preserved;
3. an inode backstop (:func:`os.path.samefile` over the candidate and its
   existing ancestors) catches any alias family the string layer missed —
   samefile is alias-proof and strictly read-only.

POSIX: the alias layer is a no-op passthrough (the spellings are
Windows-specific); the guard reduces to resolve + normcase + prefix, plus
the samefile backstop. Every probe here is read-only — the guard never
writes, anywhere.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

__all__ = [
    "admin_share_reachable",
    "canonical_journal_path",
    "is_within_production_home",
    "production_accidental_spellings",
    "production_alias_spellings",
    "production_journal_home",
    "same_journal_target",
]


def production_journal_home() -> Path:
    """The production journal home the §3 guards exist to protect."""
    return Path.home() / ".claude" / "hpc"


def _local_host_names() -> frozenset[str]:
    """Names by which a UNC path can spell THIS machine (admin-share aliases)."""
    names = {"localhost", "127.0.0.1"}
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    for candidate in (hostname, os.environ.get("COMPUTERNAME", "")):
        if candidate:
            names.add(candidate.lower())
    return frozenset(names)


def _strip_nt_alias_spellings(raw: str) -> str:
    """Map the Windows alias spellings of a local path to plain DOS form.

    POSIX passthrough — every alias here is Windows-specific. Handles the
    extended-length prefix (``\\\\?\\C:\\...``), its UNC form
    (``\\\\?\\UNC\\server\\share\\...``), and admin-share spellings of this
    machine's drives (``\\\\localhost\\C$\\...``, ``\\\\127.0.0.1\\C$\\...``,
    ``\\\\<hostname>\\C$\\...``). Anything else is returned unchanged.
    """
    if os.name != "nt":
        return raw
    s = raw.replace("/", "\\")
    low = s.lower()
    if low.startswith("\\\\?\\unc\\"):
        s = "\\\\" + s[len("\\\\?\\UNC\\") :]
    elif low.startswith("\\\\?\\"):
        s = s[len("\\\\?\\") :]
    if s.startswith("\\\\"):
        server, _, rest = s[2:].partition("\\")
        share, _, tail = rest.partition("\\")
        if (
            server.lower().rstrip(".") in _local_host_names()
            and len(share) == 2
            and share[0].isalpha()
            and share[1] == "$"
        ):
            s = f"{share[0]}:\\{tail}" if tail else f"{share[0]}:\\"
    return s


def canonical_journal_path(candidate: str | Path) -> Path:
    """The alias-free, resolved form of a journal-home candidate.

    expanduser → strip alias spellings → resolve → strip AGAIN (``resolve()``
    itself may PRODUCE a ``\\\\?\\``/UNC form when it opens an aliased path).
    For any non-aliased input this is exactly ``expanduser().resolve()`` —
    the pre-fix guard's comparison form. POSIX: expanduser + resolve.
    """
    expanded = str(Path(candidate).expanduser())
    resolved = str(Path(_strip_nt_alias_spellings(expanded)).resolve())
    return Path(_strip_nt_alias_spellings(resolved))


def _canonical_key(candidate: str | Path) -> str:
    """The normcased canonical form — the comparison key the string leg uses."""
    return os.path.normcase(str(canonical_journal_path(candidate)))


def _samefile_quiet(a: Path, b: Path) -> bool:
    """samefile that treats OS-level failure as 'cannot prove identity'.

    Strictly read-only (samefile opens both paths to compare volume +
    file-index; it never writes). An unreachable share or a missing path
    must not crash the guard — the string legs still stand.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _inode_within(candidate: Path, prod: Path) -> bool:
    """Alias-proof backstop: True when candidate IS prod or lives inside it.

    Walks the candidate and its ancestors; any member inode-identical to prod
    means the candidate reaches the production namespace no matter how it was
    spelled — including a not-yet-existing subdir, caught through its nearest
    existing ancestor. Read-only probes only (exists + samefile).
    """
    try:
        if not prod.exists():
            return False
    except OSError:
        return False
    for member in (candidate, *candidate.parents):
        try:
            if member.exists() and _samefile_quiet(member, prod):
                return True
        except OSError:
            continue
    return False


def is_within_production_home(candidate: str | Path) -> bool:
    """THE §3 containment test, alias-proof.

    True when *candidate* IS — or lives anywhere inside — the production
    journal home ``~/.claude/hpc``, under ANY spelling the OS would resolve
    to it. Two independent legs, either of which refuses: the canonical
    string test (the pre-fix guard's own normcase + prefix comparison, run on
    the de-aliased form) and the inode backstop. A SIBLING of the production
    home passes, exactly as before: journal writes land under
    ``<home>/<repo_hash>/`` and never reach the production namespace.
    """
    prod = production_journal_home()
    probe = canonical_journal_path(candidate)
    prod_key = _canonical_key(prod)
    cand_key = os.path.normcase(str(probe))
    if cand_key == prod_key or cand_key.startswith(prod_key + os.sep):
        return True
    return _inode_within(probe, canonical_journal_path(prod))


def same_journal_target(a: str | Path, b: str | Path) -> bool:
    """True when *a* and *b* name the SAME journal home (the agreement leg).

    Canonical-form equality first — two alias spellings of one target agree
    (a ``\\\\?\\``-prefixed env and its plain DOS declaration are the same
    home, not a divergence). When the forms differ, an inode test on existing
    paths catches alias families the string layer missed. Read-only.
    """
    pa, pb = canonical_journal_path(a), canonical_journal_path(b)
    if pa == pb:
        return True
    return _samefile_quiet(pa, pb)


def admin_share_reachable(spelling: str) -> bool:
    """Whether an admin-share spelling is served by this box (read-only probe).

    Tests keyed ``share/*`` skip when this is False; on a box serving the
    share they are real tests.
    """
    try:
        return os.path.exists(spelling)
    except OSError:
        return False


def production_alias_spellings() -> dict[str, str]:
    """The deliberate-alias corpus (Defect 1): every spelling MUST be refused.

    case id -> a spelling of the production home (or a subdir of it).
    Windows-only entries appear only on Windows; entries keyed ``share/*``
    reach the box through an admin share, so tests skip them when
    :func:`admin_share_reachable` is False. Shared by both test modules so
    the two public guards are pinned against the SAME corpus.
    """
    prod = production_journal_home()
    cases = {"plain": str(prod), "subdir": str(prod / "sandbox-ns")}
    if os.name != "nt":
        return cases
    drive = prod.drive  # e.g. 'C:'; '' or a UNC share when home is not a local drive
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        rest = str(prod)[len(drive) :].lstrip("\\/")
        share = f"{drive[0]}$"
        cases["ext_prefix"] = "\\\\?\\" + str(prod)
        cases["ext_prefix_subdir"] = "\\\\?\\" + str(prod / "newsub")
        cases["share/localhost"] = f"\\\\localhost\\{share}\\{rest}"
        cases["share/loopback"] = f"\\\\127.0.0.1\\{share}\\{rest}"
        cases["share/ext_unc_localhost"] = f"\\\\?\\UNC\\localhost\\{share}\\{rest}"
        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = ""
        if hostname:
            cases["share/hostname"] = f"\\\\{hostname}\\{share}\\{rest}"
    return cases


def production_accidental_spellings() -> dict[str, str]:
    """The accidental-spelling corpus: every spelling MUST stay refused.

    The pre-fix guard already refused these (the 2026-07-18 red-team's
    accidental variants); they are pinned so the alias fix can never regress
    them. Windows-semantics entries (case, trailing space/dot) appear only on
    Windows; 8.3 short-name and junction spellings ride the same resolve()
    leg these exercise (resolve canonicalizes both) and are not separately
    enumerated.
    """
    prod = production_journal_home()
    s = str(prod)
    cases = {
        "dot_segment": str(prod / "."),
        "dotdot_roundtrip": str(prod.parent / f"{prod.name}-sibling" / ".." / prod.name),
    }
    if os.name == "nt":
        cases.update(
            {
                "upper": s.upper(),
                "lower": s.lower(),
                "forward_slashes": s.replace("\\", "/"),
                "trailing_space": s + " ",
                "trailing_dot": s + ".",
            }
        )
    return cases


# Import-identity anchor: this module is loadable under several names —
# pytest's sys.path-prepend (``sandbox_guard``), the
# ``tests.integration.scheduler`` package path (a tests.contracts consumer
# importing sandbox_seed), and by-path exec from
# ``scripts/run_sandbox_proving.py``'s sibling loader. Register the bare
# spelling at exec time so any later import of it is a sys.modules hit on
# THIS object: every consumer binds the ONE guard module object, which is
# what test_sandbox_guard.py::test_guard_consumers_share_one_module_object
# asserts. (The dotted package spelling is deliberately NOT aliased:
# pre-registering a dotted name whose parent package never loaded breaks a
# later ``from tests.integration.scheduler import sandbox_guard``.)
sys.modules.setdefault("sandbox_guard", sys.modules[__name__])
