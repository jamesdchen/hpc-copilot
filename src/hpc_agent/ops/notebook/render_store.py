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

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.ops.notebook.audit_view import _render_section

if TYPE_CHECKING:
    from hpc_agent.ops.notebook.audit_view import PriorSignoff, SectionView
    from hpc_agent.ops.notebook.linked_sources import LinkedEngine

__all__ = [
    "HEADER_KEYS",
    "RenderDigest",
    "write_render",
    "render_bytes",
    "render_path",
    "read_render_header",
    "read_render_digest",
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
    grammar both :func:`read_render_header` and :func:`read_render_digest` share.
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


# --- the bounded audit-view digest (E-render, DIGEST v2) --------------------
# ``docs/design/mcp-elicitation.md`` E-render (SHIPPED 2026-07-09; DIGEST v2 amended
# same-day per RULING 2): the sign-off elicitation popup is a SIGNING surface, not a
# reading surface, so the digest carries only what serves one of three jobs — BIND
# (identity + freshness), WHY-YOUR-JUDGMENT (the tier-trigger headline, the assert
# table, the lint-flag NAMES + locations, per-hunk one-liners), and ROUTE (the
# on-disk path). The full render stays on disk for the Read pane (RULING 1: digest,
# not full render). The digest is derived from the ON-DISK render file (the
# code-authored trusted artifact the T8 gate binds), NEVER re-derived from the
# notebook ``.py`` source — the same input the human signed against.
#
# BOUNDED by construction: counts + capped, per-item-truncated lists — never the
# diff body, never an unbounded source echo. Every list carries its FULL count too
# so the popup composer (``mcp_server._render_digest_block``) can DISCLOSE how many
# were elided; a silent drop of a judgment-critical item (a failed assert) is the
# misleading-summary class the honesty rule (``mcp_server``) refuses outright.
#
# STATIC-AUDIT invariant (``audit_view``: "No execution ever happens here"): the
# trusted render carries STATIC assertions only — declared expressions, never a
# computed/execution value, and never a per-assertion pass/fail. So the assert
# table reports the DECLARED assertions and the ``tier`` the section was rendered
# at; a "computed value" the render does not hold is never fabricated (fabricating
# one would BE the misleading-summary class). See the digest-v2 spec in the design
# doc for why this honors the ruling's letter under the static-audit constraint.

#: How many declared assertions the digest lists verbatim before eliding.
_DIGEST_MAX_ASSERTIONS = 6
#: The per-assertion character cap in the digest (each entry truncated to this).
_DIGEST_MAX_ASSERTION_CHARS = 120
#: How many diff HUNK one-liners the digest lists before eliding.
_DIGEST_MAX_HUNKS = 6
#: The per-hunk-line character cap (the first changed line is truncated to this).
_DIGEST_MAX_HUNK_CHARS = 100
#: How many lint-flag name+location lines the digest lists before eliding.
_DIGEST_MAX_LINT_FLAGS = 8

#: A unified-diff hunk header ``@@ -a,b +c,d @@`` — the SOURCE-side range (``+c,d``)
#: is what the human is signing, so the one-liner names ``c … c+d-1``.
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<span>\d+))? @@")


@dataclass(frozen=True)
class RenderDigest:
    """A bounded, code-computed digest of ONE section's on-disk render (E-render v2).

    Every field is derived from the render file's own header + code-rendered body
    (the same three audit-view projections — diff-from-template, assertion table,
    lint flags — plus the identifiers), never from the notebook source. ``view_sha``
    / ``section`` / ``section_sha`` / ``audit_id`` come from the header;
    ``classification`` / ``tier`` and the counts/lists come from the body.

    The three lists are each capped and carry their FULL count beside them so the
    popup composer can disclose elision honestly:

    * ``assertions`` — capped at :data:`_DIGEST_MAX_ASSERTIONS`, each truncated to
      :data:`_DIGEST_MAX_ASSERTION_CHARS`; ``assertion_count`` is the full count.
      STATIC declarations (no per-assertion computed value exists — see the module
      note); the composer marks them unverified rather than inventing a value.
    * ``diff_hunks`` — capped at :data:`_DIGEST_MAX_HUNKS` per-hunk one-liners
      (source line range + the first changed line, truncated); ``diff_hunk_count``
      is the full count. NEVER the diff body.
    * ``lint_flags`` — capped at :data:`_DIGEST_MAX_LINT_FLAGS` ``rule @ location``
      strings (NAMES + locations, not counts); ``lint_flag_count`` is the full count.
    """

    audit_id: str
    section: str
    section_sha: str
    view_sha: str
    classification: str
    tier: str
    diff_added: int
    diff_removed: int
    diff_hunks: tuple[str, ...]
    diff_hunk_count: int
    assertion_count: int
    assertions: tuple[str, ...]
    lint_flag_count: int
    lint_flags: tuple[str, ...]
    #: The src-digest block (slice 1): one ``module @ path:lineno … (module_sha …)``
    #: string per bound engine, capped; ``linked_engine_count`` is the full count.
    #: Empty tuple / 0 when the section binds no linked sources (byte-absent block).
    linked_engine_count: int = 0
    linked_engines: tuple[str, ...] = ()
    #: The prior-sign-off advisory line (slice 3), or ``None`` when this content was
    #: not human-signed under any other audit. Display-only — never a status input.
    prior_signoff: str | None = None


def _parse_tier(text: str) -> str:
    """The tier from the section header ``## section: <slug>  [tier: <tier>]``."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## section:") and "[tier:" in stripped:
            return stripped.split("[tier:", 1)[1].rstrip("]").strip()
    return ""


def _parse_body_digest(
    text: str,
) -> tuple[str, int, int, tuple[str, ...], int, int, tuple[str, ...]]:
    """Scan a render body for its digest fields (pure, fail-soft on shape drift).

    Returns ``(classification, diff_added, diff_removed, diff_hunks,
    diff_hunk_count, assertion_count, assertions)``. Anchored on the stable
    ``_render_section`` sub-headers (``### diff-from-template`` / ``### assertions``
    / ``### lint flags``); a render body carries exactly ONE section, so there is no
    cross-section ambiguity. Diff stats count added/removed lines INSIDE the
    ```` ```diff ```` fence only (never the ``+++``/``---`` file labels); each HUNK
    (``@@ … @@``) yields a one-liner naming its source line range + first changed
    line; assertion entries are the ``- L<n>: …`` lines, each truncated. The diff
    BODY never leaves this function — only counts + hunk one-liners.
    """
    classification = ""
    diff_added = diff_removed = 0
    assertions: list[str] = []
    hunks: list[str] = []
    hunk_count = 0
    pending_hunk_range: str | None = None
    pending_hunk_first: str | None = None

    def _flush_hunk() -> None:
        nonlocal pending_hunk_range, pending_hunk_first
        if pending_hunk_range is None:
            return
        first = pending_hunk_first or "(no changed line)"
        if len(hunks) < _DIGEST_MAX_HUNKS:
            hunks.append(f"{pending_hunk_range}: {first}")
        pending_hunk_range = pending_hunk_first = None

    section: str | None = None
    in_diff_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- classification:"):
            classification = stripped.split(":", 1)[1].strip()
            continue
        if stripped == "### diff-from-template":
            section, in_diff_fence = "diff", False
            continue
        if stripped == "### assertions":
            _flush_hunk()
            section = "assertions"
            continue
        if stripped == "### lint flags":
            _flush_hunk()
            section = "lint"
            continue
        if section == "diff":
            if stripped.startswith("```"):
                if in_diff_fence:  # closing fence — finalize the last hunk
                    _flush_hunk()
                in_diff_fence = not in_diff_fence
                continue
            if in_diff_fence:
                m = _HUNK_HEADER_RE.match(stripped)
                if m is not None:
                    _flush_hunk()
                    hunk_count += 1
                    start = int(m.group("start"))
                    span = int(m.group("span")) if m.group("span") else 1
                    end = start + max(span, 1) - 1
                    pending_hunk_range = f"L{start}" if start == end else f"L{start}–{end}"
                    continue
                if line.startswith("+") and not line.startswith("+++"):
                    diff_added += 1
                    if pending_hunk_first is None:
                        pending_hunk_first = _trunc_hunk_line("+" + line[1:].strip())
                elif line.startswith("-") and not line.startswith("---"):
                    diff_removed += 1
                    if pending_hunk_first is None:
                        pending_hunk_first = _trunc_hunk_line("-" + line[1:].strip())
        elif section == "assertions" and stripped.startswith("- L"):
            entry = stripped[2:]
            if len(entry) > _DIGEST_MAX_ASSERTION_CHARS:
                entry = entry[: _DIGEST_MAX_ASSERTION_CHARS - 1] + "…"
            if len(assertions) < _DIGEST_MAX_ASSERTIONS:
                assertions.append(entry)
    _flush_hunk()
    assertion_count = _count_assertion_lines(text)
    return (
        classification,
        diff_added,
        diff_removed,
        tuple(hunks),
        hunk_count,
        assertion_count,
        tuple(assertions),
    )


def _trunc_hunk_line(text: str) -> str:
    """Truncate one changed diff line to :data:`_DIGEST_MAX_HUNK_CHARS`."""
    return text if len(text) <= _DIGEST_MAX_HUNK_CHARS else text[: _DIGEST_MAX_HUNK_CHARS - 1] + "…"


def _count_assertion_lines(text: str) -> int:
    """The TOTAL count of ``- L…`` assertion lines in the assertions block."""
    count = 0
    in_assertions = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### assertions":
            in_assertions = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_assertions = False
            continue
        if in_assertions and stripped.startswith("- L"):
            count += 1
    return count


def _parse_lint_flags(text: str) -> tuple[tuple[str, ...], int]:
    """The lint flags as ``rule @ location`` NAME strings (bounded) + full count.

    Each rendered flag line is ``- <canonical-json-of-the-finding>``
    (``_render_section``); this parses that JSON and lifts the ``rule`` NAME plus a
    location (``evidence.line`` → ``L<n>``, else the finding/evidence ``section``/
    ``slug``). A finding whose JSON does not parse falls back to a truncated raw
    line — fail-soft, never a crash. ``(none)`` renders without a ``- `` and so
    counts zero. The list is capped at :data:`_DIGEST_MAX_LINT_FLAGS`; the count is
    full (:func:`_count_lint_flag_lines`) so the composer discloses elision.
    """
    flags: list[str] = []
    in_lint = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### lint flags":
            in_lint = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_lint = False
            continue
        if in_lint and stripped.startswith("- ") and len(flags) < _DIGEST_MAX_LINT_FLAGS:
            flags.append(_lint_flag_label(stripped[2:]))
    return tuple(flags), _count_lint_flag_lines(text)


def _lint_flag_label(raw: str) -> str:
    """``rule @ location`` for one rendered lint-flag JSON blob (fail-soft)."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _trunc_hunk_line(raw)
    if not isinstance(obj, dict):
        return _trunc_hunk_line(raw)
    rule = str(obj.get("rule") or "flag")
    evidence = obj.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    line_no = evidence.get("line")
    if isinstance(line_no, int):
        return f"{rule} @ L{line_no}"
    where = obj.get("section") or evidence.get("slug") or evidence.get("name")
    return f"{rule} @ {where}" if where else rule


def _count_lint_flag_lines(text: str) -> int:
    """The count of ``- …`` lint-flag lines in the lint-flags block (``(none)``
    is rendered WITHOUT a leading ``- `` and so counts as zero)."""
    count = 0
    in_lint = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### lint flags":
            in_lint = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_lint = False
            continue
        if in_lint and stripped.startswith("- "):
            count += 1
    return count


#: How many linked-engine lines the digest lists before eliding (matches the
#: render's own cap; the disclosed ``… +N more`` line is read for the full count).
_DIGEST_MAX_LINKED_ENGINES = 6

#: The render's engine-elision disclosure line ``- … +N more`` — parsed so the
#: digest's ``linked_engine_count`` recovers the full total (visible + N).
_LINKED_MORE_RE = re.compile(r"^- … \+(?P<more>\d+) more$")


def _parse_linked_engines(text: str) -> tuple[tuple[str, ...], int]:
    """The src-digest engine lines (bounded) + full count, from a render body.

    Each rendered engine is a ``- <module> @ …`` line under ``### linked sources``;
    the disclosed ``- … +N more`` elision line is NOT an engine — it is READ to
    recover the FULL count (visible engines + N) so ``linked_engine_count`` is the
    full total, like ``assertion_count`` / ``lint_flag_count``. ``(none)`` never
    appears — the block is byte-absent when there are no engines — so a body without
    the header yields ``((), 0)``. Fail-soft: unknown shapes contribute nothing.
    """
    engines: list[str] = []
    count = 0
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### linked sources":
            in_block = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_block = False
            continue
        if not in_block or not stripped.startswith("- "):
            continue
        m = _LINKED_MORE_RE.match(stripped)
        if m is not None:
            count += int(m.group("more"))
            continue
        count += 1
        if len(engines) < _DIGEST_MAX_LINKED_ENGINES:
            engines.append(_trunc_hunk_line(stripped[2:]))
    return tuple(engines), count


def _parse_prior_signoff(text: str) -> str | None:
    """The prior-sign-off advisory line (slice 3) from a render body, or ``None``.

    The single ``- identical content signed …`` line under ``### prior sign-off``;
    absent block → ``None`` (fail-soft)."""
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "### prior sign-off":
            in_block = True
            continue
        if stripped.startswith("### ") or stripped.startswith("## "):
            in_block = False
            continue
        if in_block and stripped.startswith("- "):
            return stripped[2:]
    return None


def read_render_digest(path: Path) -> RenderDigest | None:
    """Read a section render off disk and compute its bounded digest, or ``None``.

    Fail-soft exactly like :func:`read_render_header`: an absent/unreadable file
    or an unparseable header reads ``None`` (the caller discloses a reason and
    degrades — never a crash, never an unmarked silent fallback). Reads the file
    ONCE; the digest is over the code-authored render bytes only.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    header = _parse_header(text)
    if header is None:
        return None
    (
        classification,
        diff_added,
        diff_removed,
        diff_hunks,
        diff_hunk_count,
        assertion_count,
        assertions,
    ) = _parse_body_digest(text)
    lint_flags, lint_flag_count = _parse_lint_flags(text)
    linked_engines, linked_engine_count = _parse_linked_engines(text)
    prior_signoff = _parse_prior_signoff(text)
    return RenderDigest(
        audit_id=header["audit_id"],
        section=header["section"],
        section_sha=header["section_sha"],
        view_sha=header["view_sha"],
        classification=classification,
        tier=_parse_tier(text),
        diff_added=diff_added,
        diff_removed=diff_removed,
        diff_hunks=diff_hunks,
        diff_hunk_count=diff_hunk_count,
        assertion_count=assertion_count,
        assertions=assertions,
        lint_flag_count=lint_flag_count,
        lint_flags=lint_flags,
        linked_engine_count=linked_engine_count,
        linked_engines=linked_engines,
        prior_signoff=prior_signoff,
    )
