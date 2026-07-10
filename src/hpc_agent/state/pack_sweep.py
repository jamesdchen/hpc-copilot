"""Generic pack-manifest re-sealing from a declarative sweep recipe (pack-refresh).

Design origin: ``docs/design/domain-packs.md`` drift log, "RULED corrections
(2026-07-10)" — *the pack gate MAY auto-remedy; latency is to be OBLITERATED*.
On drift the remedy chain re-seals only the manifests that are actually stale and
re-binds them, journaled old→new. This module is the re-seal half: **pure hashing
over the pack's declarative sweep recipe**, which is DATA, not domain logic. DP2
holds — core NEVER executes a pack's build/check script; it reads the recipe
(``sweep.json``) as data, resolves its globs, hashes raw bytes, and rebuilds the
manifest in the exact canonical form the pack's own ``build_*_pack.py`` emits.

**The recipe (``sweep.json``) is a build RECIPE, not sealed content** — a Makefile
is not one of its own targets — so it never appears in the integrity set. Its
shape (the harxhar-clean ``packs/{quant,rv}/sweep.json`` convention, the live
precedent)::

    {"name": <slug>, "version": <str>, "seams": {<seam>: <relpath>, …},
     "fills_slots": [<slug>, …], "pack_files": [<relpath>, …],
     "sweep": [<glob>, …]}

The manifest is the sorted union of ``pack_files`` and every file the ``sweep``
globs resolve to (each glob relative to the pack root — the recipe's own dir),
each hashed as raw bytes, serialized ``json.dumps(indent=2, sort_keys=True)+"\\n"``
— byte-identical to ``build_quant_pack.py::build_manifest_dict`` /
``_serialize`` so a pack's own ``--check`` CI still agrees after core re-seals.

**Staleness is SEMANTIC, not byte-exact** (so whitespace-only churn never forces a
rebuild, and editing one pack's content never marks another stale — the
"editing rv content must not force a quant rebuild" requirement): a manifest is
stale iff its recorded ``{name, version, seams, fills_slots, {path: sha}}`` differs
from what the recipe resolves to on disk right now.

Pure I/O + structure validation: no ``_wire`` import, no SSH, no scheduler, no
``importlib``/``exec``/``eval`` — the ``state/pack.py`` posture.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.state.pack import SEAM_NAMES, sha256_bytes, sha256_file
from hpc_agent.state.scopes import validate_tag

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "RECIPE_FILENAME",
    "SweepRecipe",
    "ResealOutcome",
    "recipe_path_for",
    "load_recipe",
    "resolve_recipe_files",
    "fresh_manifest_dict",
    "serialize_manifest",
    "reseal_manifest",
]

#: The convention: a pack's sweep recipe lives beside its manifest, named
#: ``sweep.json`` (the harxhar-clean live layout). ``recipe_path_for`` derives it
#: from a manifest relpath, so a caller never names the recipe separately.
RECIPE_FILENAME = "sweep.json"


@dataclass(frozen=True)
class SweepRecipe:
    """A pack's declarative build recipe — identity + globs only, never content.

    ``pack_files`` are pack-root-relative relpaths always sealed; ``sweep`` are
    globs (pack-root-relative) whose current hits are additionally sealed — the
    "sweep docs at pack build" flow. ``name``/``version``/``seams``/``fills_slots``
    are copied verbatim into the rebuilt manifest (core never interprets them).
    """

    name: str
    version: str
    seams: dict[str, str]
    fills_slots: tuple[str, ...]
    pack_files: tuple[str, ...]
    sweep: tuple[str, ...]


@dataclass(frozen=True)
class ResealOutcome:
    """The result of a re-seal attempt over one pack manifest.

    ``stale`` is the SEMANTIC verdict (recipe-resolved content vs the on-disk
    manifest). ``wrote`` is True only when ``stale`` and the canonical bytes were
    written. ``old_manifest_sha`` / ``new_manifest_sha`` are the manifest FILE's
    raw-bytes shas before/after (the pack identity shas the bind records);
    ``old_manifest_sha`` is ``None`` when no readable manifest existed. The file
    deltas name exactly which sealed files moved — the drift the archive records.
    """

    recipe_found: bool
    stale: bool
    wrote: bool
    old_manifest_sha: str | None
    new_manifest_sha: str | None
    files_before: dict[str, str] = field(default_factory=dict)
    files_after: dict[str, str] = field(default_factory=dict)

    @property
    def added_files(self) -> list[str]:
        return sorted(set(self.files_after) - set(self.files_before))

    @property
    def removed_files(self) -> list[str]:
        return sorted(set(self.files_before) - set(self.files_after))

    @property
    def changed_files(self) -> list[str]:
        return sorted(
            p
            for p in set(self.files_before) & set(self.files_after)
            if self.files_before[p] != self.files_after[p]
        )


def recipe_path_for(manifest_path: Path) -> Path:
    """The sweep recipe path for a manifest — its sibling :data:`RECIPE_FILENAME`."""
    return manifest_path.parent / RECIPE_FILENAME


def _require_str(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise errors.SpecInvalid(f"sweep recipe {what} must be a non-empty string, got {value!r}")
    return value


def load_recipe(recipe_path: Path) -> SweepRecipe:
    """Read + shape-validate a ``sweep.json`` recipe (loud :class:`errors.SpecInvalid`).

    Validates STRUCTURE only, the ``state/pack.py`` posture: a slug ``name``, a
    non-empty ``version``, ``seams`` keys inside the closed :data:`SEAM_NAMES`
    vocabulary pointing at string relpaths, ``fills_slots`` slugs, and
    ``pack_files``/``sweep`` lists of non-empty relpath/glob strings. Never
    interprets a declared value's meaning.
    """
    try:
        text = recipe_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"sweep recipe {str(recipe_path)!r} is unreadable ({exc}); a pack that "
            "opted into refresh with a missing/unreadable recipe is broken, not a "
            "silent pass"
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(
            f"sweep recipe {str(recipe_path)!r} is not valid JSON ({exc})"
        ) from exc
    if not isinstance(data, dict):
        raise errors.SpecInvalid(f"sweep recipe {str(recipe_path)!r} must be a JSON object")

    name = _require_str(data.get("name"), what="'name'")
    validate_tag(name)  # slug — it keys the pack journal path
    version = _require_str(data.get("version"), what="'version'")

    raw_seams = data.get("seams", {})
    if not isinstance(raw_seams, dict):
        raise errors.SpecInvalid("sweep recipe 'seams' must be an object")
    seams: dict[str, str] = {}
    for seam, rel in raw_seams.items():
        if seam not in SEAM_NAMES:
            raise errors.SpecInvalid(
                f"sweep recipe declares unknown seam {seam!r}; the seam vocabulary is "
                f"closed: {sorted(SEAM_NAMES)}"
            )
        seams[seam] = _require_str(rel, what=f"seams[{seam!r}]")

    def _slug_list(key: str) -> tuple[str, ...]:
        raw = data.get(key, [])
        if not isinstance(raw, list):
            raise errors.SpecInvalid(f"sweep recipe {key!r} must be a list")
        out: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str):
                raise errors.SpecInvalid(f"sweep recipe {key!r}[{i}] must be a slug string")
            validate_tag(item)
            out.append(item)
        return tuple(out)

    def _str_list(key: str) -> tuple[str, ...]:
        raw = data.get(key, [])
        if not isinstance(raw, list):
            raise errors.SpecInvalid(f"sweep recipe {key!r} must be a list")
        out: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str) or not item:
                raise errors.SpecInvalid(
                    f"sweep recipe {key!r}[{i}] must be a non-empty relpath/glob string"
                )
            out.append(item)
        return tuple(out)

    return SweepRecipe(
        name=name,
        version=version,
        seams=seams,
        fills_slots=_slug_list("fills_slots"),
        pack_files=_str_list("pack_files"),
        sweep=_str_list("sweep"),
    )


def _rel_from_pack_root(abspath: str, pack_root: Path) -> str:
    """Pack-root-relative POSIX relpath (keeps ``../../writeup/…`` for swept docs).

    Mirrors ``build_quant_pack.py::_rel_from_pack_root`` so a re-seal produces the
    same relpaths the pack's own build script would.
    """
    return os.path.relpath(abspath, pack_root).replace(os.sep, "/")


def resolve_recipe_files(recipe: SweepRecipe, pack_root: Path) -> list[str]:
    """The sorted union of ``pack_files`` + every ``sweep`` glob hit (pack-relative).

    Each glob resolves against *pack_root* (the recipe's own dir); a glob is data
    core matches, never a predicate it evaluates. Mirrors
    ``build_quant_pack.py::build_manifest_dict``'s ``sorted({*pack_files,
    *_sweep_docs(sweep)})``.
    """
    hits: set[str] = set(recipe.pack_files)
    for pattern in recipe.sweep:
        for abspath in glob.glob(str(pack_root / pattern)):
            p = pack_root / os.path.relpath(abspath, pack_root)
            if p.is_file():
                hits.add(_rel_from_pack_root(abspath, pack_root))
    return sorted(hits)


def fresh_manifest_dict(recipe: SweepRecipe, pack_root: Path) -> dict[str, Any]:
    """Rebuild the manifest dict from the recipe + current on-disk bytes (raw sha).

    Byte-for-byte the shape ``build_quant_pack.py`` emits: ``files`` is the sorted
    resolved list, each with its raw-bytes SHA-256; ``name``/``version``/``seams``/
    ``fills_slots`` are copied from the recipe. A listed file that is unreadable is
    loud (``errors.SpecInvalid``) — a recipe naming a vanished pack file is a
    broken setup, never a silent seal.
    """
    relpaths = resolve_recipe_files(recipe, pack_root)
    files: list[dict[str, str]] = []
    for rel in relpaths:
        target = pack_root / rel
        try:
            sha = sha256_file(target)
        except (OSError, UnicodeDecodeError) as exc:
            raise errors.SpecInvalid(
                f"sweep recipe for pack {recipe.name!r} names file {rel!r}, which is "
                f"unreadable ({exc}); cannot re-seal a manifest over a vanished file"
            ) from exc
        files.append({"path": rel, "sha256": sha})
    return {
        "name": recipe.name,
        "version": recipe.version,
        "files": files,
        "seams": dict(recipe.seams),
        "fills_slots": list(recipe.fills_slots),
    }


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """The canonical manifest text — matches ``build_quant_pack.py::_serialize``."""
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def _semantic(manifest: dict[str, Any]) -> tuple[Any, ...]:
    """The staleness-relevant projection: identity + the {path: sha} integrity set.

    Whitespace / key-order differences never move it; a moved file sha, an added /
    removed swept file, or a changed name/version/seams/fills_slots does.
    """
    files = manifest.get("files")
    file_map = (
        {f["path"]: f["sha256"] for f in files if isinstance(f, dict) and "path" in f}
        if isinstance(files, list)
        else {}
    )
    seams = manifest.get("seams")
    fills = manifest.get("fills_slots")
    return (
        manifest.get("name"),
        manifest.get("version"),
        tuple(sorted(seams.items())) if isinstance(seams, dict) else (),
        tuple(fills) if isinstance(fills, list) else (),
        tuple(sorted(file_map.items())),
    )


def _read_on_disk_manifest(manifest_path: Path) -> dict[str, Any] | None:
    """Parse the on-disk manifest as a raw dict, or ``None`` if absent/unreadable.

    A missing/unreadable/non-JSON manifest reads as ``None`` → the re-seal treats
    it as stale and writes a fresh one (a pack whose manifest vanished is stale by
    construction, not a silent pass).
    """
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def reseal_manifest(manifest_path: Path, recipe_path: Path) -> ResealOutcome:
    """Re-seal *manifest_path* from *recipe_path* IF it is semantically stale.

    Reads the recipe (loud on a broken recipe), rebuilds the manifest from current
    on-disk bytes, and compares SEMANTICALLY to the manifest on disk. Not stale →
    write nothing, ``stale=False``. Stale → write the canonical bytes and report
    the old/new manifest shas + the file-level deltas. Pure hashing — DP2 holds
    (no pack code runs).
    """
    recipe = load_recipe(recipe_path)  # loud on missing/unreadable/bad recipe
    pack_root = manifest_path.parent
    fresh = fresh_manifest_dict(recipe, pack_root)

    on_disk = _read_on_disk_manifest(manifest_path)
    files_before = (
        {f["path"]: f["sha256"] for f in on_disk.get("files", []) if isinstance(f, dict)}
        if on_disk is not None and isinstance(on_disk.get("files"), list)
        else {}
    )
    files_after = {f["path"]: f["sha256"] for f in fresh["files"]}

    old_sha = None
    if manifest_path.is_file():
        try:
            old_sha = sha256_file(manifest_path)
        except (OSError, UnicodeDecodeError):
            old_sha = None

    stale = on_disk is None or _semantic(on_disk) != _semantic(fresh)
    if not stale:
        return ResealOutcome(
            recipe_found=True,
            stale=False,
            wrote=False,
            old_manifest_sha=old_sha,
            new_manifest_sha=old_sha,
            files_before=files_before,
            files_after=files_after,
        )

    text = serialize_manifest(fresh)
    manifest_path.write_text(text, encoding="utf-8")
    new_sha = sha256_bytes(text.encode("utf-8"))
    return ResealOutcome(
        recipe_found=True,
        stale=True,
        wrote=True,
        old_manifest_sha=old_sha,
        new_manifest_sha=new_sha,
        files_before=files_before,
        files_after=files_after,
    )
