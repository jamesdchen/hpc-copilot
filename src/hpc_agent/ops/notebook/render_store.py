"""The TRUSTED-DISPLAY render store — content-addressed section render files.

Design origin: ``docs/design/notebook-audit.md`` (the v1.5 trusted-display lock,
user-approved 2026-07-07 — "prose recruits, gates guarantee"). The audit view an
agent relays into chat is MODEL-CARRIED and unforceable; the trusted artifact is
a CONTENT-ADDRESSED RENDER FILE written by CODE. This module owns that file: the
code-written path, the machine-parseable header block, and the fail-soft parse
the T8 sign-off gate reads back.

The load-bearing property lives in the gate (``ops/decision/journal.py``'s
``_assert_signoff_authorship``), NOT here: a sign-off may not land unless the
render file addressed by the resolved ``view_sha`` exists on disk and was
produced against CURRENT source (its header ``section_sha`` equals the gate's
freshly-recomputed section sha). This module only writes and reads; it enforces
nothing. Same trust model as every store: the filesystem is code-written, so
tool-surface enforcement is the guarantee and filesystem forgery is out of scope
(the ``journal.py`` honest-limit paragraph).

Path scheme (content-addressed by the per-section ``view_sha``)::

    <experiment>/.hpc/renders/<audit_id>/<slug>.<view_sha12>.md

Each file OPENS with a header block of machine-parseable HTML-comment lines —
invisible in a rendered markdown view but exactly recoverable by
:func:`read_render_header` — carrying ``{audit_id, section, section_sha,
view_sha}``, followed by a blank line and the code-rendered markdown projection
of the section (the same deterministic ``_render_section`` the whole-view
markdown uses). Bytes are DETERMINISTIC: no timestamps, no absolute paths — the
same section view yields a byte-identical file, so a re-render is a no-op and the
content address is stable across platforms.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.notebook.audit_view import _render_section

if TYPE_CHECKING:
    from hpc_agent.ops.notebook.audit_view import PriorSignoff, SectionView
    from hpc_agent.ops.notebook.linked_sources import LinkedEngine

__all__ = [
    "HEADER_KEYS",
    "write_render",
    "render_bytes",
    "render_path",
    "read_render_header",
]

#: The header keys every render file carries — the gate cross-checks all four.
HEADER_KEYS: tuple[str, ...] = ("audit_id", "section", "section_sha", "view_sha")

#: One header line: ``<!-- hpc-render <key>: <value> -->``. HTML comments so the
#: block is invisible in a rendered markdown view yet exactly machine-parseable.
_HEADER_PREFIX = "hpc-render"
_HEADER_LINE_RE = re.compile(
    r"^<!--\s*" + re.escape(_HEADER_PREFIX) + r"\s+(?P<key>[a-z_]+):\s*(?P<value>.*?)\s*-->$"
)

#: How many chars of the ``view_sha`` name the file (the content address). 12 is
#: the ``JournalLayout.repo_hash`` precedent — collision-safe for a section pool.
_VIEW_SHA_ADDRESS_LEN = 12


def _renders_root(experiment_dir: Path, audit_id: str) -> Path:
    """``<experiment>/.hpc/renders/<audit_id>/`` (not created)."""
    return RepoLayout(experiment_dir).hpc / "renders" / audit_id


def render_path(experiment_dir: Path, *, audit_id: str, section: str, view_sha: str) -> Path:
    """The content-addressed path a section's render file lives at.

    Addressed by the per-section ``view_sha`` (its first
    :data:`_VIEW_SHA_ADDRESS_LEN` chars) so the file the sign-off gate looks up is
    keyed on exactly what the human was shown. Pure — creates nothing.
    """
    name = f"{section}.{view_sha[:_VIEW_SHA_ADDRESS_LEN]}.md"
    return _renders_root(experiment_dir, audit_id) / name


def _render_bytes(*, audit_id: str, view: SectionView) -> str:
    """The deterministic file body: the header block + the section markdown.

    No timestamps, no absolute paths — same inputs → byte-identical output.
    """
    header = [
        f"<!-- {_HEADER_PREFIX} audit_id: {audit_id} -->",
        f"<!-- {_HEADER_PREFIX} section: {view.slug} -->",
        f"<!-- {_HEADER_PREFIX} section_sha: {view.section_sha} -->",
        f"<!-- {_HEADER_PREFIX} view_sha: {view.view_sha} -->",
        "",
    ]
    body = _render_section(view)
    return "\n".join([*header, *body]).rstrip() + "\n"


def _enrich_view(
    experiment_dir: Path,
    audit_id: str,
    view: SectionView,
    source_relpath: str | None = None,
) -> SectionView:
    """Return *view* enriched with the src digest (slice 1) + prior sign-off (slice 3).

    The ONE seat that holds both the experiment dir and the audit id, so it is where
    the two PRESENTATION-ONLY blocks are resolved. Fully FAIL-OPEN: any error (no
    opt-in, unreadable source, a corrupt journal) returns *view* unchanged, so a
    standalone or broken audit renders exactly as it did before this feature (the
    byte-absent pin). Neither block enters ``view_sha`` — the content address is
    unchanged, only the human-readable body gains lines.

    *source_relpath* (notebook-audit 6a) is the caller-declared audited-source
    ``.py`` relpath — the seat that lets a STANDALONE audit (no interview.json
    ``audited_source`` block) take the recorded-config path too: its roots are
    the journaled ``notebook-audit-config`` record
    (:func:`~hpc_agent.ops.notebook.canonical.read_recorded_config`'s second
    seat), and the source path the caller names here. ``None`` (every pre-6a
    call site) keeps the interview-block seat only — byte-identical behavior.
    """
    engines: tuple[LinkedEngine, ...] = ()
    prior: PriorSignoff | None = None
    try:
        engines = _resolve_linked_engines(
            experiment_dir, audit_id, view, source_relpath=source_relpath
        )
    except Exception:  # noqa: BLE001 — enrichment is advisory; never fail a render
        engines = ()
    try:
        prior = _find_prior_signoff(experiment_dir, audit_id, view)
    except Exception:  # noqa: BLE001 — advisory disclosure only, fail-open
        prior = None
    if not engines and prior is None:
        return view
    return replace(view, linked_engines=engines, prior_signoff=prior)


def _resolve_linked_engines(
    experiment_dir: Path,
    audit_id: str,
    view: SectionView,
    *,
    source_relpath: str | None = None,
) -> tuple[LinkedEngine, ...]:
    """The section's imports resolved to engine digests under the audit's ``source_roots``.

    Roots come from the RECORDED audit config via the ONE canonical reader
    (:func:`~hpc_agent.ops.notebook.canonical.read_recorded_config`) — which
    covers BOTH seats: interview.json's ``audited_source`` block WINS when
    present (the opt-in path owns the config), else the journaled
    ``notebook-audit-config`` record a STANDALONE audit wrote via
    ``notebook-record-config``. The source ``.py`` relpath comes from the
    explicit *source_relpath* when the caller names one (what it actually
    rendered — the standalone audit's only source-path seat), else the
    interview block's ``source``. Parses the source, finds THIS section by slug,
    and routes its imports through the ONE resolver
    (``linked_sources.resolve_section_engines``). Returns ``()`` when neither
    seat names a source path, or the section is absent, or no roots are
    recorded — the fail-open default.

    Pre-6a this gated EVERYTHING on the interview block, so a standalone audit
    (roots in the journal, no interview block) never reached the recorded-config
    path and rendered no src digest at all — the gap the *source_relpath* seat
    closes.
    """
    from hpc_agent.ops.notebook.canonical import (
        read_interview_audited_source,
        read_recorded_config,
    )
    from hpc_agent.ops.notebook.linked_sources import resolve_section_engines
    from hpc_agent.state.audit_source import parse_percent_source

    # Roots from the ONE canonical reader — interview seat wins, else the
    # standalone audit's journaled config (the recorded-config path a
    # standalone audit must also take).
    cfg = read_recorded_config(experiment_dir, audit_id)
    root_dirs = [
        (Path(r) if Path(r).is_absolute() else experiment_dir / r) for r in cfg.source_roots
    ]
    if not root_dirs:
        return ()
    source_rel: str | None = source_relpath
    if not isinstance(source_rel, str) or not source_rel:
        block = read_interview_audited_source(experiment_dir, audit_id)
        block_source = block.get("source") if isinstance(block, dict) else None
        source_rel = block_source if isinstance(block_source, str) and block_source else None
    if not source_rel:
        return ()
    source_path = Path(source_rel)
    if not source_path.is_absolute():
        source_path = experiment_dir / source_path
    if not source_path.is_file():
        return ()
    parsed = parse_percent_source(source_path.read_text(encoding="utf-8"))
    section = next((s for s in parsed.sections if s.slug == view.slug), None)
    if section is None:
        return ()
    return tuple(resolve_section_engines(section.source, experiment_dir, root_dirs))


def _find_prior_signoff(
    experiment_dir: Path, audit_id: str, view: SectionView
) -> PriorSignoff | None:
    """A DIFFERENT audit's HUMAN sign-off of this section's exact current content, or ``None``.

    Routes through the ONE ledger reader
    (:func:`~hpc_agent.state.notebook_audit.read_signoff_ledger`, wave-3 piece 1) —
    a bounded, fail-open scan of every ``.hpc/notebooks/*.decisions.jsonl`` for a
    ``notebook-sign-off`` whose ``section_sha`` equals THIS section's sha under a
    DIFFERENT audit. Returns the EARLIEST such sign-off with a count of DISTINCT
    prior audits (the recurrence signal piece 4 nudges on) as a
    :class:`~hpc_agent.ops.notebook.audit_view.PriorSignoff` — advisory display
    only, never a status/clearing input.
    """
    from hpc_agent.ops.notebook.audit_view import PriorSignoff
    from hpc_agent.state.notebook_audit import read_signoff_ledger

    entries = read_signoff_ledger(
        experiment_dir, content_sha=view.section_sha, exclude_audit_id=audit_id
    )
    if not entries:
        return None
    distinct_audits = {e.audit_id for e in entries}
    earliest = entries[0]  # the ledger returns entries ascending by ts
    return PriorSignoff(
        date=earliest.ts[:10],
        audit_id=earliest.audit_id,
        count=len(distinct_audits),
        actor=earliest.actor,
    )


def write_render(
    experiment_dir: Path,
    *,
    audit_id: str,
    view: SectionView,
    source_relpath: str | None = None,
) -> Path:
    """Write *view*'s content-addressed render file and return its path.

    Creates the ``.hpc/renders/<audit_id>/`` parent lazily (the ``RepoLayout``
    idiom) and writes the header + markdown at the ``view_sha``-addressed path.
    Before rendering, the view is ENRICHED (:func:`_enrich_view`, fail-open) with
    the src digest (slice 1) + a prior-sign-off advisory (slice 3) — both
    presentation-only, so ``view_sha`` (and the content address) are unchanged; a
    section with no linked sources and no prior sign-off renders byte-identically.

    *source_relpath* (notebook-audit 6a) names the audited-source ``.py`` the
    caller rendered — the seat that lets a STANDALONE audit (no interview.json
    ``audited_source`` opt-in) take the recorded-config path too, so its
    journaled ``notebook-audit-config`` roots enrich the src digest. ``None``
    (every pre-6a call site) keeps the interview-block seat only — the byte-absent
    pin is untouched.

    The write is idempotent by construction: the bytes are deterministic and the
    path is content-addressed — and a file already carrying the identical bytes
    is left UNTOUCHED, never rewritten. The skip is load-bearing, not an
    optimization: the file's mtime is the sign-off gate's temporal anchor (a
    candidate utterance must post-date the render the human saw — run-#12
    finding 10), so a re-view of an unchanged section (same source, same journals)
    must not move it.
    """
    view = _enrich_view(experiment_dir, audit_id, view, source_relpath=source_relpath)
    path = render_path(experiment_dir, audit_id=audit_id, section=view.slug, view_sha=view.view_sha)
    content = _render_bytes(audit_id=audit_id, view=view)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == content:
            return path
    except OSError:
        pass  # unreadable existing file → fall through to the rewrite
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def render_bytes(*, audit_id: str, view: SectionView) -> str:
    """The PUBLIC deterministic-render entrypoint: the exact bytes :func:`write_render` lays down.

    The sanctioned way to obtain the KNOWN code-rendered trusted-display payload
    IN-PROCESS (the conformance kit's capability-4 reference battery imports THIS as
    the byte-for-byte expectation, exactly as the fence kit imports
    ``scheduler_write_fence.fenced_in_command``) — never the package-private
    ``_render_bytes``. Same inputs → byte-identical output (no timestamps, no
    absolute paths), so the content address (``view.view_sha``) is stable and a
    substitution is detectable by a plain byte compare.
    """
    return _render_bytes(audit_id=audit_id, view=view)


def _parse_header(text: str) -> dict[str, str] | None:
    """Parse a render file's leading header block from *text*, or ``None``.

    Reads the leading run of ``<!-- hpc-render <key>: <value> -->`` comment lines
    (blank lines tolerated) and stops at the first markdown body line. Returns the
    mapping only when ALL of :data:`HEADER_KEYS` are present. The single header
    grammar :func:`read_render_header` reads.
    """
    header: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue  # blank line inside/after the header block
        m = _HEADER_LINE_RE.match(stripped)
        if m is None:
            break  # first non-header line — the markdown body starts here
        header[m.group("key")] = m.group("value")
    if not all(key in header for key in HEADER_KEYS):
        return None
    return header


def read_render_header(path: Path) -> dict[str, str] | None:
    """Parse a render file's header block, or ``None`` (fail-soft).

    Reads the leading run of ``<!-- hpc-render <key>: <value> -->`` comment lines
    (blank lines tolerated) and stops at the first markdown body line. Returns the
    mapping only when ALL of :data:`HEADER_KEYS` are present; any missing key, an
    unreadable file, or a header-less file reads ``None`` — a soft absence the gate
    turns into a loud, path-naming refusal (a malformed header must never read as a
    valid trusted display).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_header(text)
