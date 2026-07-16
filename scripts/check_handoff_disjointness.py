"""Verify a handoff package's ``unit-specs.json`` is safe to dispatch to a swarm.

A handoff package (``docs/plans/<program>/unit-specs.json``, template at
``docs/plans/_TEMPLATE-handoff/``) hands file-disjoint ``units`` to parallel
implementation agents that never coordinate at runtime. The load-bearing
invariant is that no two agents touch the same seam. This checker mechanizes the
three failure modes that break it (all observed on real swarm runs):

(a) **Unchecked convergence** — two units of the SAME wave claim the same file.
    Same-wave units dispatch in parallel, so a shared ``files`` entry means two
    agents editing one file with no coordination (the calibration/SGE collision).
    ==> ERROR (exit nonzero). Cross-wave overlap is legal (the file is edited in
    sequence across waves) and only WARNs, printing the wave order so the
    rebase-first dependency is visible. A directory claim in one unit that
    *contains* another same-wave unit's file claim is a softer smell and WARNs.

(b) **Claim drift / typo** — a ``files`` entry is a typo of a real file, so the
    claim silently points at nothing and the unit's true footprint is unguarded.
    A missing path whose intended sibling clearly exists (an existing parent dir
    holding a near-identically-named file) is a confident typo ==> ERROR. A
    missing path that is plausibly a brand-new file (marked ``(new)``, or landing
    in a not-yet-created subtree, or with no similar sibling) only WARNs — new
    files are the normal case and must not fail the gate.

(c) **In-flight overlap at dispatch** (``--against-worktree``) — the working
    tree is already dirty on a file some unit claims (the wave-0 partial-work
    reset). Every ``git status`` path intersecting a claim is listed; in this
    mode an intersection is a dispatch gate ==> ERROR, because dispatching a
    swarm onto an already-dirty seam silently overlaps in-flight work.

(d) ``forbidden_files`` overlapping another unit's ``files`` is INTENTIONAL (that
    is exactly what forbidden_files is for) and is never an error.

Prose annotations in parens are stripped; entries that are prose after stripping
(``doctor module``, ``slash_commands twin of SKILL.md``) are skipped. Globs
(``tests/infra/test_io*.py``) and directory claims (``tests/daemon/``) are
supported.

Usage::

    python scripts/check_handoff_disjointness.py docs/plans/<program>/unit-specs.json
    python scripts/check_handoff_disjointness.py            # all docs/plans/*/unit-specs.json
    python scripts/check_handoff_disjointness.py <path> --against-worktree

Exit code is nonzero if any ERROR fires (same-wave collision, confident typo, or
— in ``--against-worktree`` — a worktree intersection). WARNs never change the
exit code. Fire paths are exercised in
``tests/scripts/test_check_handoff_disjointness.py``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Extensions that make a bare token read as a concrete file path (not prose).
_PATH_EXTS = frozenset(
    {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini", ".js", ".sh"}
)


# --------------------------------------------------------------------------- #
# Entry parsing
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Entry:
    """A single normalized ``files`` / ``forbidden_files`` entry.

    ``kind`` is one of ``file`` | ``dir`` | ``glob`` | ``prose``. Only the first
    three participate in overlap/existence checks; ``prose`` is carried for
    diagnostics but otherwise skipped.
    """

    raw: str
    path: str  # normalized posix, annotation stripped ("" for prose)
    kind: str
    is_new: bool


def _strip_annotation(raw: str) -> tuple[str, bool]:
    """Strip a trailing ``( ... )`` annotation; report whether it marked ``new``.

    ``"scripts/foo.py (new)"`` -> ``("scripts/foo.py", True)``.
    ``"infra/cluster_status.py (EXCLUSIVE)"`` -> ``("infra/cluster_status.py", False)``.
    """
    text = raw.strip()
    is_new = False
    while text.endswith(")") and "(" in text:
        open_idx = text.rfind("(")
        annotation = text[open_idx + 1 : -1]
        if "new" in annotation.lower():
            is_new = True
        text = text[:open_idx].strip()
    return text, is_new


def parse_entry(raw: str) -> Entry:
    """Classify one raw ``files`` string into an :class:`Entry`."""
    stripped, is_new = _strip_annotation(raw)
    norm = stripped.replace("\\", "/").strip()

    # Prose: nothing left, or embedded spaces with no glob and no clear path shape.
    if not norm:
        return Entry(raw, "", "prose", is_new)
    if " " in norm:
        return Entry(raw, "", "prose", is_new)

    if "*" in norm or "?" in norm or "[" in norm:
        return Entry(raw, norm, "glob", is_new)
    if norm.endswith("/"):
        return Entry(raw, norm.rstrip("/"), "dir", is_new)

    suffix = Path(norm).suffix
    if suffix in _PATH_EXTS or "/" in norm:
        return Entry(raw, norm, "file", is_new)
    # Single bare token, no extension, no separator -> prose ("doctor module").
    return Entry(raw, "", "prose", is_new)


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)

    def extend(self, other: Report) -> None:
        self.errors.extend(other.errors)
        self.warns.extend(other.warns)

    @property
    def ok(self) -> bool:
        return not self.errors


def _load_units(specs: dict) -> list[dict]:
    """Return the real units (top-level ``_``-prefixed pseudo-units ignored)."""
    units = specs.get("units", [])
    return [u for u in units if isinstance(u, dict) and u.get("unit_id")]


def _within(child: str, parent_dir: str) -> bool:
    """True if posix path *child* is at or under directory *parent_dir*."""
    return child == parent_dir or child.startswith(parent_dir + "/")


def _matches(entry: Entry, other: str) -> bool:
    """True if *entry* (file/dir/glob) covers the plain posix path *other*."""
    if entry.kind == "file":
        return entry.path == other
    if entry.kind == "dir":
        return _within(other, entry.path)
    if entry.kind == "glob":
        if fnmatchcase(other, entry.path):
            return True
        return fnmatchcase(Path(other).name, Path(entry.path).name)
    return False


# ---- (a) same-wave overlap ------------------------------------------------- #
def check_wave_overlap(units: list[dict], label: str) -> Report:
    """Same-wave shared claim = ERROR; cross-wave = WARN; dir-containment = WARN."""
    rep = Report()
    # Collect (unit_id, wave, Entry) for every concrete files-claim.
    claims: list[tuple[str, object, Entry]] = []
    for u in units:
        for raw in u.get("files", []):
            e = parse_entry(raw)
            if e.kind != "prose":
                claims.append((u["unit_id"], u.get("wave"), e))

    n = len(claims)
    for i in range(n):
        uid_a, wave_a, ea = claims[i]
        for j in range(i + 1, n):
            uid_b, wave_b, eb = claims[j]
            if uid_a == uid_b:
                continue
            exact = bool(ea.path) and ea.path == eb.path and ea.kind == eb.kind
            # Directory-vs-file (or dir-vs-dir) containment.
            contain = (
                not exact
                and {ea.kind, eb.kind} <= {"file", "dir"}
                and (
                    (ea.kind == "dir" and _within(eb.path, ea.path))
                    or (eb.kind == "dir" and _within(ea.path, eb.path))
                )
            )
            if exact:
                shared = ea.path
                if wave_a == wave_b:
                    rep.errors.append(
                        f"{label}: same-wave collision on {shared!r} — units {uid_a} and "
                        f"{uid_b} both claim it in wave {wave_a!r}. Same-wave units dispatch "
                        f"in parallel; a shared file = two uncoordinated agents on one seam. "
                        f"Split the claim or move one unit to a later wave."
                    )
                else:
                    early_id, early_w, late_id, late_w = (
                        (uid_a, wave_a, uid_b, wave_b)
                        if str(wave_a) <= str(wave_b)
                        else (uid_b, wave_b, uid_a, wave_a)
                    )
                    rep.warns.append(
                        f"{label}: cross-wave overlap on {shared!r} — {early_id} "
                        f"(wave {early_w!r}) then {late_id} (wave {late_w!r}). Legal if edited "
                        f"in sequence; the later unit must rebase-first on the earlier land."
                    )
            elif contain and wave_a == wave_b:
                big, small = (ea, eb) if ea.kind == "dir" else (eb, ea)
                big_id, small_id = (uid_a, uid_b) if ea.kind == "dir" else (uid_b, uid_a)
                rep.warns.append(
                    f"{label}: same-wave containment — unit {big_id} claims directory "
                    f"{big.path!r}/ which contains unit {small_id}'s claim {small.path!r} "
                    f"(wave {wave_a!r}). Confirm the directory owner delegates that file, or "
                    f"narrow the directory claim."
                )
    return rep


# ---- (b) path reality / typo ---------------------------------------------- #
def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """Bounded Levenshtein distance (returns ``cap`` once the bound is exceeded)."""
    if abs(len(a) - len(b)) > cap:
        return cap
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            best = min(best, v)
        if best > cap:
            return cap
        prev = cur
    return min(prev[-1], cap)


def _looks_like_typo(rel: str, repo_root: Path) -> str | None:
    """If *rel* is a missing file that resembles a real sibling, return that sibling.

    A confident typo: the parent directory exists, the file does not, and a
    sibling file's basename is within edit-distance 2 (or a separator/case
    variant) of the claimed basename. Returns ``None`` otherwise (genuinely new
    files land here and only WARN).
    """
    p = repo_root / rel
    if p.exists():
        return None
    parent = p.parent
    if not parent.is_dir():
        return None  # whole new subtree, not a typo of an existing sibling
    name = p.name
    norm_name = name.lower().replace("_", "").replace("-", "").replace(".", "")
    for sib in parent.iterdir():
        if not sib.is_file():
            continue
        sib_name = sib.name
        if sib_name == name:
            return None
        norm_sib = sib_name.lower().replace("_", "").replace("-", "").replace(".", "")
        if norm_sib == norm_name or _levenshtein(name, sib_name, cap=3) <= 2:
            return sib_name
    return None


def check_paths(units: list[dict], repo_root: Path, label: str) -> Report:
    """Confident typo = ERROR; plausibly-new missing path = WARN."""
    rep = Report()
    for u in units:
        for raw in u.get("files", []):
            e = parse_entry(raw)
            if e.kind == "prose":
                continue
            if e.kind == "glob":
                # A glob claim that matches nothing under an existing parent is suspect.
                parent = (repo_root / e.path).parent
                if parent.is_dir():
                    hits = fnmatch.filter([c.name for c in parent.iterdir()], Path(e.path).name)
                    if not hits and not e.is_new:
                        rep.warns.append(
                            f"{label}: unit {u['unit_id']} glob {e.raw!r} matches no file "
                            f"under {parent.as_posix()} (new files, or a stale glob?)."
                        )
                continue
            rel = e.path
            target = repo_root / rel
            if target.exists():
                continue
            if e.is_new:
                continue
            sibling = _looks_like_typo(rel, repo_root)
            if sibling is not None:
                rep.errors.append(
                    f"{label}: unit {u['unit_id']} claims {e.raw!r}, which does not exist but "
                    f"closely resembles the real file {sibling!r} in the same directory — a "
                    f"typo'd claim silently guards nothing (claim-drift class). Fix the path, "
                    f"or mark it ' (new)' if it is genuinely new."
                )
            else:
                rep.warns.append(
                    f"{label}: unit {u['unit_id']} claims {e.raw!r}, which is not in the tree "
                    f"(a new file? mark it ' (new)' to silence this)."
                )
    return rep


# ---- (c) worktree intersection -------------------------------------------- #
def git_worktree_files(repo_root: Path) -> list[str]:
    """Posix paths reported by ``git status --porcelain`` (adds, mods, renames)."""
    out = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain", "-z"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git status failed: {out.stderr.strip()}")
    return _parse_porcelain_z(out.stdout)


def _parse_porcelain_z(payload: str) -> list[str]:
    """Parse NUL-delimited ``git status --porcelain -z`` into posix paths.

    Rename/copy entries emit two NUL-separated fields (new then old); both are
    reported so a rename source that a unit claims is still caught.
    """
    files: list[str] = []
    tokens = payload.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        status, path = tok[:2], tok[3:]
        files.append(path.replace("\\", "/"))
        if status[0] in ("R", "C"):
            i += 1  # consume the rename/copy source in the next token
            if i < len(tokens) and tokens[i]:
                files.append(tokens[i].replace("\\", "/"))
        i += 1
    return files


def check_worktree(units: list[dict], worktree_files: list[str], label: str) -> Report:
    """List every dirty worktree path that intersects a unit claim = ERROR (gate)."""
    rep = Report()
    for wt in worktree_files:
        for u in units:
            for raw in u.get("files", []):
                e = parse_entry(raw)
                if e.kind == "prose":
                    continue
                if _matches(e, wt):
                    rep.errors.append(
                        f"{label}: worktree file {wt!r} intersects unit {u['unit_id']}'s claim "
                        f"{e.raw!r} — in-flight work already owns this seam. Land or stash it "
                        f"before dispatching (wave-0 rule: dirty = claimed, rebase-first)."
                    )
    return rep


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def check_spec_file(
    spec_path: Path,
    repo_root: Path,
    against_worktree: bool = False,
    worktree_files: list[str] | None = None,
) -> Report:
    """Run every check against one ``unit-specs.json`` and return the merged report."""
    label = (
        spec_path.relative_to(repo_root).as_posix()
        if _is_relative(spec_path, repo_root)
        else spec_path.name
    )
    rep = Report()
    try:
        specs = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        rep.errors.append(f"{label}: could not load ({exc})")
        return rep
    units = _load_units(specs)
    if not units:
        rep.warns.append(f"{label}: no units (template or empty package) — skipped.")
        return rep

    rep.extend(check_wave_overlap(units, label))
    rep.extend(check_paths(units, repo_root, label))
    if against_worktree:
        wt = worktree_files if worktree_files is not None else git_worktree_files(repo_root)
        rep.extend(check_worktree(units, wt, label))
    return rep


def _is_relative(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def discover_spec_files(repo_root: Path) -> list[Path]:
    """All ``docs/plans/*/unit-specs.json`` except template dirs (``_``-prefixed)."""
    plans = repo_root / "docs" / "plans"
    if not plans.is_dir():
        return []
    return sorted(p for p in plans.glob("*/unit-specs.json") if not p.parent.name.startswith("_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "spec",
        nargs="?",
        help="Path to a unit-specs.json (default: every docs/plans/*/unit-specs.json).",
    )
    parser.add_argument(
        "--against-worktree",
        action="store_true",
        help="Also flag `git status` files that intersect a unit claim (dispatch gate).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO,
        help="Repository root (default: this script's repo).",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.spec:
        spec_files = [Path(args.spec).resolve()]
    else:
        spec_files = discover_spec_files(repo_root)
        if not spec_files:
            print("no unit-specs.json found under docs/plans/*/", file=sys.stderr)
            return 0

    combined = Report()
    for sf in spec_files:
        combined.extend(check_spec_file(sf, repo_root, against_worktree=args.against_worktree))

    for w in combined.warns:
        print(f"WARN  {w}", file=sys.stderr)
    for e in combined.errors:
        print(f"ERROR {e}", file=sys.stderr)

    checked = ", ".join(sf.name for sf in spec_files)
    if combined.errors:
        print(
            f"check_handoff_disjointness: {len(combined.errors)} error(s), "
            f"{len(combined.warns)} warning(s) across {len(spec_files)} package(s)",
            file=sys.stderr,
        )
        return 1
    print(
        f"check_handoff_disjointness: OK — {len(combined.warns)} warning(s) "
        f"across {len(spec_files)} package(s) [{checked}]",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
