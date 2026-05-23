"""Generate / check primitive frontmatter from the ``@primitive`` registry.

Step 4 of the C′ design: the registry is the SoT for the structured
metadata embedded in each ``docs/primitives/<name>.md`` frontmatter.
This script renders the registry's view of each primitive and either
prints the diff (default) or rewrites the YAML frontmatter block in
place (``--write``). The ``--check`` flag exits non-zero if running
the writer would produce a diff (CI gate).

The body of each primitive doc — everything after the closing ``---``
line — is human-owned prose and is preserved verbatim. Only the YAML
frontmatter between the leading and closing ``---`` markers is
regenerated.

The registry doesn't yet model every frontmatter field (CLI invocation
strings, free-form ``inputs`` / ``outputs`` documentation, prose
``description`` text, exit-code descriptions). Those fields are
read from the existing frontmatter and round-tripped untouched. Only
the fields the registry owns (``name``, ``verb``, ``side_effects``,
``idempotent``, ``idempotency_key``, ``error_codes``) are rewritten.

Usage::

    uv run python scripts/build_primitive_frontmatter.py            # diff
    uv run python scripts/build_primitive_frontmatter.py --check    # CI gate
    uv run python scripts/build_primitive_frontmatter.py --write    # apply
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _shared import REPO_ROOT  # noqa: E402

from hpc_agent._internal.primitive import get_registry, register_primitives  # noqa: E402

PRIMITIVES_DIR = REPO_ROOT / "docs" / "primitives"

# Field order. Mirrors the existing hand-authored layout so the diff is
# minimal on first run.
_FIELD_ORDER = (
    "name",
    "verb",
    "inputs",
    "outputs",
    "side_effects",
    "idempotent",
    "idempotency_key",
    "error_codes",
    "backed_by",
    "exit_codes",
)


def _render_side_effects(meta) -> list:
    """Project structured SideEffects into the disk YAML shape.

    Frontmatter conventions vary: some use the ``- kind: target`` map
    form, others use ``- kind`` strings, others use prose like ``- ssh:
    cluster reachable``. The registry stores ``(kind, target)`` tuples;
    we render as a list of one-key maps so the YAML diff is structured
    and stable.
    """
    out = []
    for se in meta.side_effects:
        if se.target:
            out.append({se.kind: se.target})
        else:
            out.append(se.kind)
    return out


def _render_error_codes(meta, fm_existing: dict) -> list:
    """Reconstruct the ``error_codes`` list.

    Frontmatter ``error_codes`` carries category + retry_safe + prose
    description per code; the registry only carries the class refs.
    Look up each registered error class on the existing frontmatter
    list to recover the prose; otherwise emit ``code: <code>`` only.
    """
    fm_codes = {}
    for entry in fm_existing.get("error_codes") or []:
        if isinstance(entry, dict) and "code" in entry:
            fm_codes[entry["code"]] = entry
    out = []
    for cls in meta.error_codes:
        code = getattr(cls, "error_code", None)
        if code and code in fm_codes:
            out.append(fm_codes[code])
        elif code:
            out.append(
                {
                    "code": code,
                    "category": getattr(cls, "category", "internal"),
                    "retry_safe": bool(getattr(cls, "retry_safe", False)),
                }
            )
    return out


def _build_frontmatter(meta, fm_existing: dict) -> dict:
    """Compose a frontmatter dict from the registry meta + the prose
    fields (inputs / outputs / backed_by / exit_codes) the registry
    doesn't yet model.
    """
    fm: dict = {
        "name": meta.name,
        "verb": meta.verb,
    }
    if "inputs" in fm_existing:
        fm["inputs"] = fm_existing["inputs"]
    if "outputs" in fm_existing:
        fm["outputs"] = fm_existing["outputs"]
    fm["side_effects"] = _render_side_effects(meta)
    fm["idempotent"] = bool(meta.idempotent)
    # Registry is SoT. Always overwrite from the decorator: prose
    # explanations the human authored on disk are no longer round-tripped
    # — they belong in the doc body, not the frontmatter.
    fm["idempotency_key"] = meta.idempotency_key if meta.idempotency_key is not None else "none"
    fm["error_codes"] = _render_error_codes(meta, fm_existing)
    # backed_by is now fully registry-owned. ``cli`` comes from the
    # decorator's ``cli=`` kwarg (None for Python-only primitives, which
    # we render as the legacy human-readable marker so existing readers
    # stay happy). ``python`` is the canonical entry-point pointer derived
    # from the func's qualified name.
    from hpc_agent.cli._dispatch import cli_to_invocation_string

    cli_str = cli_to_invocation_string(meta.name, meta.cli)
    fm["backed_by"] = {
        "cli": cli_str if cli_str is not None else "(none — Python-only primitive)",
        "python": f"{meta.func.__module__}.{meta.func.__qualname__}",
    }
    if "exit_codes" in fm_existing:
        fm["exit_codes"] = fm_existing["exit_codes"]
    return fm


def _serialize(fm: dict) -> str:
    """YAML-serialize the frontmatter dict in deterministic field order."""
    ordered = {k: fm[k] for k in _FIELD_ORDER if k in fm}
    # Preserve any unknown keys (forward-compat) at the end.
    for k, v in fm.items():
        if k not in ordered:
            ordered[k] = v
    return yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False, allow_unicode=True)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml_block, body_after_closing_marker)."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end != -1:
        return text[4:end], text[end + len("\n---\n") :]
    # EOF-terminated frontmatter: ``\n---`` at end of file with no
    # trailing newline. Without this fallback the function returned
    # ``("", text)`` and the rewriter would later PREPEND a fresh
    # frontmatter block, duplicating it.
    if text.endswith("\n---"):
        return text[4 : -len("\n---")], ""
    return "", text


def _render_doc(meta, body: str, fm_existing: dict) -> str:
    fm = _build_frontmatter(meta, fm_existing)
    yaml_block = _serialize(fm).rstrip()
    return f"---\n{yaml_block}\n---\n{body}"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    register_primitives()
    drift: list[tuple[str, str, str]] = []  # (path, old, new)
    missing: list[str] = []  # primitive names with no docs/primitives/<name>.md
    registry = get_registry()
    for name, meta in sorted(registry.items()):
        path = PRIMITIVES_DIR / f"{name}.md"
        if not path.is_file():
            # Scaffold a placeholder doc so the registry doesn't have
            # silent orphans. Frontmatter is registry-owned; the body
            # is a one-line stub the human (or the next pass) fills in.
            missing.append(name)
            stub_body = f"# {name}\n\n_Documentation pending._\n"
            new = _render_doc(meta, stub_body, {})
            drift.append((str(path), "", new))
            continue
        old = path.read_text(encoding="utf-8")
        fm_yaml, body = _split_frontmatter(old)
        try:
            fm_existing = yaml.safe_load(fm_yaml) or {} if fm_yaml else {}
        except yaml.YAMLError:
            fm_existing = {}
        new = _render_doc(meta, body, fm_existing)
        if old != new:
            drift.append((str(path), old, new))

    if not drift:
        print(f"frontmatter up to date ({len(registry)} primitives)")
        return 0

    if check:
        print(
            f"ERROR: {len(drift)} primitive frontmatter file(s) out of date — "
            "run scripts/build_primitive_frontmatter.py --write to regenerate",
            file=sys.stderr,
        )
        for path, _, _ in drift:
            print(f"  {path}", file=sys.stderr)
        return 1

    if write:
        for path, _, new in drift:
            Path(path).write_text(new, encoding="utf-8")
            print(f"  wrote {path}")
        print(f"regenerated {len(drift)} primitive frontmatter file(s)")
        return 0

    # Default: print a diff summary so the human can preview without
    # touching the tree.
    import difflib

    for path, old, new in drift:
        print(f"--- a/{path}")
        print(f"+++ b/{path}")
        diff = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            n=2,
        )
        sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
