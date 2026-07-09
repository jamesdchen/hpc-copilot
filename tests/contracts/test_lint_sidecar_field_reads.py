"""Cross-file lint: every ``sidecar.get(<key>)`` reads a key the writer writes.

The per-run sidecar at ``<exp>/.hpc/runs/<run_id>.json`` is written
exclusively by :func:`hpc_agent.state.runs.write_run_sidecar` (initial
write), :func:`hpc_agent.state.runs.update_run_sidecar_job_ids`
(post-qsub finalize), and the cluster-side dispatcher
(``execution/mapreduce/dispatch.py``) which populates the per-task
``tasks`` block. Run-lifecycle fields (``status``, ``last_status``,
``lifecycle_state``, ``ssh_target``, ``job_name``, …) live on the
journal :class:`RunRecord` at ``~/.claude/hpc/<repo_hash>/runs/<run_id>.json``,
NOT on the per-experiment sidecar.

Reading those fields from a sidecar always returns ``None``/empty,
silently degrading the call site. See the audit-fixes commit history
for four bugs of this exact shape (``update-run-constraints``,
``campaign-health``, …).

The lint walks every ``.py`` under ``src/``,
finds ``<receiver>.get(<literal>)`` and ``<receiver>[<literal>]`` calls
where the receiver name matches a sidecar-pattern, and flags any key
not in the authoritative allowed set.

This is a TYPE LINT in spirit: it would be unnecessary if
``read_run_sidecar`` returned a typed model instead of a raw dict.
Until that refactor lands (see ``SidecarRecord`` in
:mod:`hpc_agent.state.runs`), this lint is the cheap insurance.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Authoritative allowed-keys set. Derived from ``state/runs.py``:
#   * v1 required header fields,
#   * the ``_V2_CONFIG_FIELDS`` tuple (v2 config snapshot),
#   * runtime-added blocks (``tasks`` from the dispatcher,
#     ``wave_map``/``extra``/``job_ids`` from the writer).
_V1_REQUIRED = frozenset(
    [
        "sidecar_schema_version",
        "run_id",
        "cmd_sha",
        "hpc_agent_version",
        "submitted_at",
        "executor",
        "result_dir_template",
        "task_count",
        "tasks_py_sha",
    ]
)
# Mirror _V2_CONFIG_FIELDS in state/runs.py. Kept inline (not imported)
# so the lint surfaces the drift if the SoT changes shape — a deliberate
# duplicate-the-list-for-redundancy choice.
_V2_CONFIG = frozenset(
    [
        "cluster",
        "profile",
        "campaign_id",
        "project",
        "remote_path",
        "resources",
        "env",
        "env_group",
        "service_env",
        "constraints",
        "gpu_fallback",
        "max_retries",
        "runtime",
        "auto_retry",
        "aggregate_defaults",
        "results",
        # summary_artifact (F-J): per-task summary filename the reducer +
        # completion counting read; absent → metrics.json via
        # resolved_summary_artifact.
        "summary_artifact",
        "trial_tokens",
        "trial_params",
        "parent_run_ids",
        "node_sha",
        "data_sha",
        # data_manifest_sha (data-manifest amendment 0b): the manifest-doc
        # identity of the declared input roots at submit — the fingerprint's
        # data-identity dimension reads it.
        "data_manifest_sha",
        # packs (domain-packs T10): opaque {pack, version, sha, manifest} echoes
        # of the bound packs at submit; export-dossier seals the named files.
        "packs",
        "env_hash",
        # scopes (rigor primitives): the caller-attached opaque evidence-scope
        # tags the reduction gate + look ledger key on.
        "scopes",
        # reproduces (reproduction-receipt): run_id of the ORIGINAL a deliberate
        # reproduction re-runs; find_run_by_cmd_sha's reproduction_of lever
        # reads it to skip a prior repro of the same original.
        "reproduces",
        # audited_source (notebook-audit T14): opaque {source, template,
        # audit_id} echo of interview.json's audit-trail identity, stamped
        # at resolve after the graduation gate passes; export-dossier reads
        # it to seal the audit trail.
        "audited_source",
    ]
)
_RUNTIME_WRITTEN = frozenset(
    [
        "wave_map",
        "extra",
        "job_ids",
        # ``tasks`` is populated by execution/mapreduce/dispatch.py at task
        # runtime — the per-task block carrying exit_code/preempt/etc.
        "tasks",
    ]
)
ALLOWED_SIDECAR_KEYS = _V1_REQUIRED | _V2_CONFIG | _RUNTIME_WRITTEN

# Variable names that, by convention, hold a sidecar dict. The
# subject-imports lint ensures this convention is uniform across the
# codebase, so a tight allowlist beats AST flow-analysis here.
_SIDECAR_RECEIVERS = frozenset(["sidecar", "sc", "sidecar_data", "side_car"])

# Files where ``sidecar``/``sc``/etc. names refer to something other
# than a per-run sidecar (e.g. test fixtures that build adjacent
# concepts). Keep this list tight; every entry is a deliberate exception.
_EXCLUDE_FILES = frozenset(
    [
        # The writer itself — defines what a sidecar is.
        "src/hpc_agent/state/runs.py",
        # Dispatcher writes the per-task ``tasks`` block; it constructs
        # sidecar entries rather than reading them.
        "src/hpc_agent/execution/mapreduce/dispatch.py",
    ]
)


def _python_files() -> list[Path]:
    out: list[Path] = []
    for root in (REPO_ROOT / "src",):
        if root.is_dir():
            out.extend(p for p in root.rglob("*.py") if p.is_file())
    return out


def _violations_in_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(lineno, receiver_name, key)`` for every flagged read."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel in _EXCLUDE_FILES:
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []

    out: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        # <receiver>.get(<literal>) — match arg 0 only.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _SIDECAR_RECEIVERS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            key = node.args[0].value
            if key not in ALLOWED_SIDECAR_KEYS:
                out.append((node.lineno, node.func.value.id, key))

        # <receiver>[<literal>] — Subscript form.
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in _SIDECAR_RECEIVERS
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            key = node.slice.value
            if key not in ALLOWED_SIDECAR_KEYS:
                out.append((node.lineno, node.value.id, key))

    return out


def test_sidecar_reads_only_reference_written_keys() -> None:
    """Every ``sidecar.get("<key>")`` reads a key ``write_run_sidecar``,
    ``update_run_sidecar_job_ids``, or the cluster-side dispatcher
    actually writes. Keys outside that set return ``None`` silently —
    the bug class that motivated this lint.
    """
    violations: dict[str, list[tuple[int, str, str]]] = {}
    for path in _python_files():
        hits = _violations_in_file(path)
        if hits:
            violations[path.relative_to(REPO_ROOT).as_posix()] = hits

    if violations:
        bullets: list[str] = []
        for rel, hits in sorted(violations.items()):
            for ln, recv, key in hits:
                bullets.append(f"  {rel}:{ln}: {recv}.get/[{key!r}]")
        raise AssertionError(
            "sidecar reads found for keys NOT written by "
            "write_run_sidecar / update_run_sidecar_job_ids / dispatch.py "
            "(read returns None, silently degrading the call site):\n"
            + "\n".join(bullets)
            + "\n\nFix options:\n"
            "  * If the field belongs on the journal (status / "
            "ssh_target / job_name / last_status), load the "
            "RunRecord via hpc_agent.state.journal.load_run instead.\n"
            "  * If the field SHOULD be on the sidecar, add it to "
            "_V2_CONFIG_FIELDS in state/runs.py (and to the lint's "
            "ALLOWED_SIDECAR_KEYS — same SoT).\n"
            "  * If the read is dead-code fallback (``.get(x) or "
            "fallback``), delete the dict-read."
        )


def test_allowed_keys_set_mirrors_writer_module() -> None:
    """Sanity: this lint's ``_V2_CONFIG`` matches the writer's
    ``_V2_CONFIG_FIELDS``. A drift means the SoT moved without an
    update here — the lint would then under-/over-shoot."""
    src = (REPO_ROOT / "src/hpc_agent/state/runs.py").read_text(encoding="utf-8")
    # Match ``_V2_CONFIG_FIELDS: tuple[str, ...] = (\n ... \n)`` —
    # closing paren must be on its own line so inline comments
    # containing parens (e.g. ``# (paths, logs)``) don't truncate the
    # capture.
    block_match = re.search(
        r"_V2_CONFIG_FIELDS:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\((.*?)\n\)",
        src,
        re.DOTALL,
    )
    assert block_match is not None, "could not locate _V2_CONFIG_FIELDS in writer"
    block = block_match.group(1)
    # Capture only quoted items at the start of each line (the tuple
    # entries), NOT quoted strings nested inside inline ``# ...`` comments
    # (e.g. ``# str — "uv" or omitted`` for the ``runtime`` entry would
    # otherwise smuggle ``"uv"`` into the set as a fake field name).
    on_disk = frozenset(re.findall(r'^\s*"([^"]+)",', block, re.MULTILINE))
    if on_disk != _V2_CONFIG:
        missing = on_disk - _V2_CONFIG
        extra = _V2_CONFIG - on_disk
        raise AssertionError(
            "this lint's _V2_CONFIG drifted from state/runs.py:_V2_CONFIG_FIELDS. "
            f"missing here: {sorted(missing)}; extra here: {sorted(extra)}. "
            "Sync the two lists."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
