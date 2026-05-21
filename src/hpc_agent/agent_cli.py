"""Command-line interface — the agent surface.

Designed to be invoked by automation (external orchestrator agents via
a Bash-style tool, cron, scripts). Conventions:

- Stdout is exclusively a single-line JSON envelope. Exception:
  ``capabilities --full`` emits a plain-text ``llms-full`` dump (one-shot
  LLM context loading, analogous to ``--help``). Every other invocation
  preserves the JSON-envelope contract.
- Stderr carries free-form diagnostic prose (e.g. ``[dispatch] ERROR: …``
  emitted by ``hpc_agent.mapreduce.dispatch`` and ``…map.combiner``); it is
  intended for humans tailing logs. Do not parse it as JSON.
- Exit codes: 0 success, 1 user error, 2 cluster/network error, 3 internal.
- Every subcommand accepts ``--experiment-dir`` (defaults to CWD).
- Subcommands with non-trivial inputs accept ``--spec path/to/spec.json``.

The full schema for each subcommand is documented in ``docs/reference/cli-spec.md``
and shipped as JSON Schema files under ``hpc_agent/schemas/``.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import subprocess
import sys
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

import hpc_agent
from hpc_agent import errors, runner
from hpc_agent._internal import session
from hpc_agent.state.discover import discover_executors

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_CLUSTER_ERROR = 2
EXIT_INTERNAL = 3

# error_code → exit code mapping. Stable contract; documented in docs/reference/cli-spec.md.
_EXIT_CODE_BY_CATEGORY = {
    "user": EXIT_USER_ERROR,
    "cluster": EXIT_CLUSTER_ERROR,
    "network": EXIT_CLUSTER_ERROR,
    "internal": EXIT_INTERNAL,
}


# ─── envelope helpers ──────────────────────────────────────────────────────


def _emit(envelope: dict[str, Any]) -> None:
    """Print a single-line JSON envelope to stdout."""
    print(json.dumps(envelope, sort_keys=True), flush=True)


@functools.cache
def _meta_idempotent(name: str) -> bool:
    """Look up a primitive's idempotency declaration from the catalog.

    B4 rewire: callers used to hardcode ``_ok(idempotent=True/False, ...)``
    which forked the truth between the @primitive decorator (consumed by
    docs / lint) and the runtime envelope (consumed by caller policy).
    Routing through the catalog collapses both to the decorator.

    Cached because the catalog walks every primitive's frontmatter on
    first call. Falls back to True on miss (consistent with the
    pre-B4 default for query-style commands; the cross-validation test
    in tests/test_idempotency.py guards against silent drift).
    """
    try:
        from hpc_agent._internal.operations import operations_catalog

        for entry in operations_catalog():
            if entry.get("name") == name:
                return bool(entry.get("idempotent", True))
    except (LookupError, KeyError, FileNotFoundError):
        # Narrow catch so programmer errors (e.g. registry queried
        # before register_primitives()) surface in main() rather than
        # being silently coerced to ``idempotent=True``.
        pass
    return True


def _ok(
    data: dict[str, Any],
    *,
    idempotent: bool | None = None,
    name: str | None = None,
    partial_errors: list[dict[str, str]] | None = None,
) -> None:
    """Emit an ok-true envelope.

    *idempotent* (B4 rewire): preferred spelling is to pass *name* — the
    primitive's catalog name — and let the envelope read the
    ``idempotent`` flag from ``operations_catalog()``. The legacy
    ``idempotent=True/False`` kwarg is still honoured for callsites that
    don't have a primitive mapping (e.g. cmd_aggregate which wraps a
    pure mapreduce reduce). When both are supplied, the explicit kwarg
    wins so callers can opt out of the catalog lookup if needed.

    *partial_errors*: optional list of ``{code, detail}`` dicts surfaced
    at the top level of the envelope — distinct from ``data.errors``.
    Used by primitives like ``inspect-cluster`` whose underlying data
    source can be partially degraded (qhost timed out, sacct
    unavailable) without the operation as a whole failing.
    """
    if idempotent is None:
        idempotent = _meta_idempotent(name) if name else True
    if name:
        from hpc_agent._internal.schema import validate_output

        validate_output(data, name)
    env: dict[str, Any] = {"ok": True, "idempotent": idempotent, "data": data}
    if partial_errors:
        env["partial_errors"] = list(partial_errors)
    _emit(env)


def _err(
    *,
    error_code: str,
    message: str,
    category: str,
    retry_safe: bool,
    remediation: str | None = None,
) -> int:
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "category": category,
        "retry_safe": retry_safe,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    _emit(payload)
    return _EXIT_CODE_BY_CATEGORY.get(category, EXIT_INTERNAL)


def _err_from_hpc(exc: errors.HpcError) -> int:
    return _err(
        error_code=exc.error_code,
        message=str(exc),
        category=exc.category,
        retry_safe=exc.retry_safe,
        remediation=exc.remediation,
    )


def _require_ssh_agent() -> int | None:
    # Cluster-touching subcommands hang silently when SSH_AUTH_SOCK is
    # missing — the most common default-empty-spawn-env failure mode
    # for external orchestrators. Fail fast with a typed error instead
    # of stalling on auth.
    if os.environ.get("SSH_AUTH_SOCK"):
        return None
    return _err_from_hpc(
        errors.SshUnreachable(
            "SSH_AUTH_SOCK is not set; cannot reach the cluster.",
            remediation=(
                "Forward SSH_AUTH_SOCK (and SSH_AGENT_PID) into the spawn "
                "environment, then run `hpc-agent preflight` to verify. "
                "See docs/integrations/CONTRACT.md for the spawn env block."
            ),
        )
    )


# ─── shared option helpers ─────────────────────────────────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _add_run_id(parser: argparse.ArgumentParser) -> None:
    """Add the canonical ``--run-id`` argument (always required)."""
    parser.add_argument("--run-id", required=True)


def _add_spec_and_dry_run(
    parser: argparse.ArgumentParser,
    *,
    schema_hint: str,
    dry_run_help: str,
) -> None:
    """Add the ``--spec`` (required) + ``--dry-run`` pair used by the
    workflow-flow subcommands (``submit-flow``, ``monitor-flow``,
    ``aggregate-flow``).

    *schema_hint* is the schema filename mentioned in the spec help
    (e.g. ``"schemas/submit_flow.input.json"``); *dry_run_help* lets
    each subcommand explain what dry-run skips.
    """
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help=f"JSON spec file ({schema_hint})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=dry_run_help,
    )


def _load_spec(spec_path: Path | None, *, schema_name: str | None = None) -> dict[str, Any]:
    """Load and (optionally) JSON-Schema-validate ``--spec`` input.

    Validation is opt-in via *schema_name* so callers without a matching
    schema (e.g. ad-hoc dicts) still work, but every CLI subcommand that
    has one in ``hpc_agent/schemas/<name>.input.json`` should pass
    it.  Validation failures map to ``SpecInvalid`` with the schema
    field path in the message — far more useful to a calling agent than
    the Python ``int("abc")`` traceback we used to surface.
    """
    if spec_path is None:
        return {}
    try:
        loaded = json.loads(spec_path.read_text())
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(f"--spec file not found: {spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(f"--spec is not valid JSON ({spec_path}): {exc}") from exc
    if not isinstance(loaded, dict):
        raise errors.SpecInvalid(f"--spec must be a JSON object; got {type(loaded).__name__}")
    if schema_name is not None:
        _validate_against_schema(loaded, schema_name)
    return loaded


def _validate_against_schema(payload: Any, schema_name: str) -> None:
    """Validate *payload* against ``hpc_agent/schemas/<schema_name>.input.json``.

    Raises :class:`errors.SpecInvalid` on schema mismatch.  When the
    ``jsonschema`` library is unavailable (older installs that haven't
    picked up the runtime dep), this falls back to a no-op so the CLI
    keeps working — schema validation is defence in depth, not the only
    line of defence (``submit_and_record`` etc. still validate inputs).

    Cross-file ``$ref`` (rare post-Pydantic-migration — most
    schemas are now self-contained with constraints inlined from
    :mod:`hpc_agent._schema_models._shared`) resolves through the
    shared registry in :mod:`hpc_agent._internal.schema`.
    """
    try:
        import jsonschema  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        # Warn once so missing-dep installs (minimal venv, broken pip
        # state) don't silently bypass the defence-in-depth layer. The
        # Pydantic-driven inner validation still runs.
        import warnings as _warnings

        _warnings.warn(
            "jsonschema not installed; skipping wire-schema validation. "
            "Install with `pip install hpc-agent[<extras>]` or `pip install jsonschema>=4.18`.",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    try:
        schema_text = (
            _resource_files("hpc_agent.schemas") / f"{schema_name}.input.json"
        ).read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        return
    schema = json.loads(schema_text)
    from hpc_agent._internal.schema import validate as _validate

    try:
        _validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise errors.SpecInvalid(
            f"--spec failed schema {schema_name}.input.json at {path}: {exc.message}"
        ) from exc


# ─── subcommand: capabilities ──────────────────────────────────────────────


# Re-exported from hpc_agent.atoms.capabilities for back-compat with
# tests that import the constant directly from agent_cli.
# back-compat: introduced 0.2.0 (atoms split). Remove in 0.4.0 —
# update tests to import from hpc_agent.atoms.capabilities directly.
from hpc_agent.atoms.capabilities import _SKILL_NAMES  # noqa: E402,F401


def _live_subcommands() -> list[str]:
    """Derive the subcommand list from the actual argparse tree.

    Replaces the hand-typed literal that used to live here — the literal
    drifted from the real subcommand set and had
    no test backing it. Walking ``parser._subparsers._group_actions[0]
    .choices`` gives the single source of truth.
    """
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def cmd_hook_install(args: argparse.Namespace) -> int:
    """Install hpc-agent's bundled Stop hooks into ~/.claude/settings.json.

    Idempotent: re-running with already-installed hooks is a no-op. Use
    ``--dry-run`` to preview the merge without writing.
    """
    from hpc_agent.hooks.install import install_hooks

    settings_path = Path(args.settings).expanduser() if args.settings else None
    summary = install_hooks(settings_path=settings_path, dry_run=args.dry_run)
    _ok(summary, name="hook-install")
    return EXIT_OK


def cmd_install_commands(args: argparse.Namespace) -> int:
    """Copy bundled slash commands + skills into ~/.claude/.

    The pip-install entry point: after ``pip install hpc-agent`` this
    wires the agent assets shipped in the wheel into Claude Code's
    user-global config dir. Idempotent (overwrites in place). Use
    ``--dry-run`` to preview without writing.
    """
    from hpc_agent.agent_assets import install_agent_assets

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    summary = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    _emit({"ok": True, "idempotent": True, "data": summary})
    return EXIT_OK


def cmd_setup(args: argparse.Namespace) -> int:
    """One-shot setup: install commands + skills, then wire the Stop hooks.

    The single entry point a new user runs after ``pip install
    hpc-agent``. Copies the bundled slash commands and skills into
    ~/.claude/ and installs hpc-agent's Stop hooks. Both steps are
    idempotent, so re-running is safe. ``--no-hooks`` skips the hook
    step; ``--dry-run`` previews both without writing.
    """
    from hpc_agent.agent_assets import install_agent_assets
    from hpc_agent.hooks.install import install_hooks

    claude_dir = Path(args.claude_dir).expanduser() if args.claude_dir else None
    assets = install_agent_assets(claude_dir=claude_dir, dry_run=args.dry_run)
    data: dict[str, Any] = {"assets": assets}
    if not args.no_hooks:
        settings_path = claude_dir / "settings.json" if claude_dir else None
        data["hooks"] = install_hooks(settings_path=settings_path, dry_run=args.dry_run)
    _emit({"ok": True, "idempotent": True, "data": data})
    return EXIT_OK


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.capabilities."""
    from hpc_agent._internal.operations import render_llms_full
    from hpc_agent.atoms.capabilities import capabilities

    if getattr(args, "full", False):
        # Human/LLM-mode: emit a multi-section text blob (NOT the JSON
        # envelope) modeled on Modal's llms-full.txt pattern. Documented
        # exception to the stdout-is-JSON contract; analogous to --help.
        sys.stdout.write(render_llms_full())
        sys.stdout.flush()
        return EXIT_OK

    _ok(capabilities(subcommands=_live_subcommands()), name="capabilities")
    return EXIT_OK


# ─── subcommand: preflight ─────────────────────────────────────────────────


def cmd_preflight(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.preflight.

    Always returns EXIT_OK on a successful primitive call; callers read
    ``data.all_ok`` from the envelope to branch. The previous form
    returned EXIT_CLUSTER_ERROR while still emitting ``ok:true``, which
    contradicts the cli-spec contract (exit code 2 implies ``ok:false``).
    """
    from hpc_agent.atoms.preflight import check_preflight

    data = check_preflight(cluster=getattr(args, "cluster", None))
    _ok(data, name="check-preflight")
    return EXIT_OK


# ─── subcommand: validate-campaign ───────────────────────────────────────


def cmd_validate_campaign(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at
    ``hpc_agent.flows.validate_campaign``.

    Exit codes:
    * ``EXIT_OK`` — overall=pass or warn (warnings don't block).
    * ``1`` — overall=fail (any error finding). The agent loop reads
      ``data.findings`` to apply suggested fixes and re-run.
    """
    from hpc_agent._schema_models.workflows.validate_campaign import ValidateCampaignSpec
    from hpc_agent.flows.validate_campaign import validate_campaign

    intent = _load_spec(args.spec, schema_name="validate_campaign")
    if not intent:
        raise errors.SpecInvalid("--spec is required for `validate-campaign`")
    try:
        spec = ValidateCampaignSpec.model_validate(intent)
    except Exception as exc:  # pydantic.ValidationError
        raise errors.SpecInvalid(str(exc)) from exc

    experiment_dir = Path(args.experiment_dir).resolve()
    report = validate_campaign(experiment_dir, spec=spec)
    _ok(report.model_dump(mode="json"), name="validate-campaign")
    # Always EXIT_OK on a successful primitive call. Callers branch on
    # ``data.overall`` (``pass``/``warn``/``fail``); exit codes are
    # reserved for envelope-level failure (``ok:false``).
    return EXIT_OK


# ─── subcommand: interview ─────────────────────────────────────────────────


def cmd_interview(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.interview."""
    from hpc_agent._schema_models.actions.interview import InterviewSpec
    from hpc_agent.atoms.interview import record_interview

    intent = _load_spec(args.spec, schema_name="interview")
    if not intent:
        raise errors.SpecInvalid("--spec is required for `interview`")
    campaign_dir = Path(args.campaign_dir).resolve()
    try:
        spec = InterviewSpec.model_validate(intent)
    except Exception as exc:  # pydantic.ValidationError
        raise errors.SpecInvalid(str(exc)) from exc
    try:
        data = record_interview(spec, campaign_dir=campaign_dir)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc
    _ok(data, name="interview")
    return EXIT_OK


# ─── subcommand: recall ────────────────────────────────────────────────────


def cmd_recall(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.recall."""
    from hpc_agent.atoms.recall import recall_campaigns, resolve_roots

    payload: dict[str, Any] = {
        "limit": int(getattr(args, "limit", 20)),
        "include_runtime": bool(getattr(args, "include_runtime", False)),
        "include_generator_stats": bool(getattr(args, "include_generator_stats", False)),
    }
    if getattr(args, "root", None):
        payload["root"] = args.root
    if getattr(args, "task_kind", None):
        payload["task_kind"] = args.task_kind
    if getattr(args, "operator", None):
        payload["operator"] = args.operator
    if getattr(args, "since", None):
        payload["since"] = args.since
    _validate_against_schema(payload, "recall")
    from hpc_agent._schema_models.queries.recall import RecallSpec

    roots = resolve_roots(getattr(args, "root", None))
    spec = RecallSpec.model_validate(payload)
    try:
        data = recall_campaigns(roots, spec=spec)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc
    _ok(data, name="recall")
    return EXIT_OK


# ─── subcommand: discover ──────────────────────────────────────────────────


def cmd_discover(args: argparse.Namespace) -> int:
    search_dirs: tuple[str, ...] | None = None
    raw = getattr(args, "search_dirs", None)
    if raw:
        # Comma-separated on the CLI; convert to the tuple the Python API
        # expects. Empty entries (e.g. trailing comma) are dropped so a
        # user typing ``--search-dirs scripts,`` doesn't accidentally
        # scan an unnamed subdir.
        parts = tuple(p.strip() for p in raw.split(",") if p.strip())
        if parts:
            search_dirs = parts
    infos = discover_executors(args.experiment_dir, search_dirs=search_dirs)
    data: dict[str, Any] = {
        "executors": [
            {
                "name": i.name,
                "path": str(i.path),
                "cli_framework": i.cli_framework,
                "has_main_guard": i.has_main_guard,
            }
            for i in infos
        ]
    }
    _ok(data, name="discover-executors")
    return EXIT_OK


# ─── subcommand: discover-reducers ─────────────────────────────────────────


def cmd_discover_reducers(args: argparse.Namespace) -> int:
    """Surface candidate reducer / aggregator scripts in the experiment repo.

    The motivating failure mode: at /aggregate-hpc time the agent writes
    a fresh QLIKE / RMSE / etc. aggregator instead of finding the one
    the user already committed. This subcommand calls
    :func:`hpc_agent.state.discover.discover_reducers` so the slash
    command can route through a CLI primitive instead of grep'ing the
    repo by hand.
    """
    from hpc_agent.state.discover import discover_reducers

    infos = discover_reducers(args.experiment_dir)
    data = {
        "reducers": [
            {
                "name": i.name,
                "path": str(i.path),
                "matches": list(i.matches),
                "docstring": i.docstring,
            }
            for i in infos
        ]
    }
    _ok(data, name="discover-reducers")
    return EXIT_OK


# ─── subcommand: plan-throughput ───────────────────────────────────────────


def cmd_plan_throughput(args: argparse.Namespace) -> int:
    from hpc_agent.atoms.plan_throughput import plan_throughput

    data = plan_throughput(
        cluster=args.cluster,
        total_tasks=args.total_tasks,
        est_task_duration_s=args.est_task_duration_s,
    )
    _ok(data, name="plan-throughput")
    return EXIT_OK


def cmd_clusters_list(_args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.clusters."""
    from hpc_agent.atoms.clusters import list_clusters

    _ok(list_clusters(), name="clusters-list")
    return EXIT_OK


def cmd_clusters_describe(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.clusters."""
    from hpc_agent.atoms.clusters import describe_cluster

    _ok(
        describe_cluster(name=args.name, strict=bool(getattr(args, "strict", False))),
        name="clusters-describe",
    )
    return EXIT_OK


# ─── subcommand: list-in-flight ────────────────────────────────────────────


# ``_last_status_age_seconds`` lives at the atom layer (it's the
# freshness helper used by both list-in-flight and the cmd_status
# adapter); re-exported here so cmd_status can keep its existing
# import-free callsite without a layering inversion.
from hpc_agent.atoms.list_in_flight import _last_status_age_seconds  # noqa: E402,F401


def cmd_list_in_flight(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.list_in_flight."""
    from hpc_agent.atoms.list_in_flight import list_in_flight

    _ok(list_in_flight(experiment_dir=args.experiment_dir), name="list-in-flight")
    return EXIT_OK


# ─── subcommand: campaign status / list ────────────────────────────────────


def cmd_campaign_status(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_status."""
    from hpc_agent.atoms.campaign_status import campaign_status

    _ok(
        campaign_status(experiment_dir=args.experiment_dir, campaign_id=args.campaign_id),
        name="campaign-status",
    )
    return EXIT_OK


def cmd_campaign_list(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_list."""
    from hpc_agent.atoms.campaign_list import campaign_list

    _ok(campaign_list(experiment_dir=args.experiment_dir), name="campaign-list")
    return EXIT_OK


def cmd_build_submit_spec(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.build_submit_spec.

    Accepts a JSON ``--spec <file>`` of resolved interview values
    (profile/cluster/ssh_target/.../cmd_sha/total_tasks/...) and emits
    the assembled + schema-validated ``submit_flow.input.json`` dict
    on stdout's ``data`` field. Pipe it straight into
    ``submit-flow --spec``.
    """
    from hpc_agent.atoms.build_submit_spec import build_submit_spec

    raw = _load_spec(args.spec, schema_name=None)
    if not isinstance(raw, dict):
        return _err(
            error_code="spec_invalid",
            message="build-submit-spec input must be a JSON object",
            category="user",
            retry_safe=False,
        )
    _validate_against_schema(raw, "build_submit_spec")
    from hpc_agent._schema_models.actions.build_submit_spec import BuildSubmitSpecInput

    try:
        bss_spec = BuildSubmitSpecInput.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        return _err(
            error_code="spec_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    spec = build_submit_spec(spec=bss_spec)
    _ok(spec, name="build-submit-spec")
    return EXIT_OK


def cmd_build_tasks_py(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.build_tasks_py.

    Accepts a JSON ``--spec <file>`` of ``{axes, flags_by_executor,
    force?}`` and scaffolds ``<experiment>/.hpc/tasks.py`` from the
    canonical Pattern 1 (cartesian product) template. Refuses to
    overwrite without ``force=true`` so hand-edited Pattern 2/3
    conversions survive across re-runs.
    """
    from hpc_agent.atoms.build_tasks_py import build_tasks_py

    raw = _load_spec(args.spec, schema_name=None)
    if not isinstance(raw, dict):
        return _err(
            error_code="spec_invalid",
            message="build-tasks-py input must be a JSON object",
            category="user",
            retry_safe=False,
        )
    _validate_against_schema(raw, "build_tasks_py")
    from hpc_agent._schema_models.actions.build_tasks_py import BuildTasksPyInput

    if args.force:
        raw["force"] = True
    try:
        spec = BuildTasksPyInput.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        return _err(
            error_code="spec_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    try:
        out = build_tasks_py(args.experiment_dir, spec=spec)
    except TypeError as exc:
        return _err(
            error_code="spec_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    _ok(out, name="build-tasks-py")
    return EXIT_OK


def cmd_decide_monitor_arm(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.monitor_arm.

    Reads a JSON ``--spec`` describing the current run state and emits
    the cron/loop/none decision + ``armed:`` line + cron_create_args.
    The slash-command epilogue copies ``armed_line`` verbatim and (when
    ``arm == "cron"``) passes ``cron_create_args`` to ``CronCreate``.
    """
    from hpc_agent.atoms.monitor_arm import decide_monitor_arm

    raw = _load_spec(args.spec, schema_name=None)
    if not isinstance(raw, dict):
        return _err(
            error_code="spec_invalid",
            message="decide-monitor-arm input must be a JSON object",
            category="user",
            retry_safe=False,
        )
    _validate_against_schema(raw, "decide_monitor_arm")
    from hpc_agent._schema_models.queries.decide_monitor_arm import DecideMonitorArmSpec

    try:
        spec = DecideMonitorArmSpec.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        return _err(
            error_code="spec_invalid",
            message=str(exc),
            category="user",
            retry_safe=False,
        )
    out = decide_monitor_arm(spec=spec)
    _ok(out, name="decide-monitor-arm")
    return EXIT_OK


def cmd_monitor_summary(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.monitor_summary."""
    from hpc_agent.atoms.monitor_summary import monitor_summary

    out = monitor_summary(args.experiment_dir, run_id=args.run_id)
    _ok(out, name="monitor-summary")
    return EXIT_OK


def cmd_suggest_setup_action(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.setup_actions."""
    from hpc_agent.atoms.setup_actions import suggest_setup_action

    _ok(suggest_setup_action(args.experiment_dir), name="suggest-setup-action")
    return EXIT_OK


def cmd_find_prior_run(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.setup_actions."""
    from hpc_agent.atoms.setup_actions import find_prior_run

    _ok(
        find_prior_run(args.experiment_dir, cmd_sha=args.cmd_sha),
        name="find-prior-run",
    )
    return EXIT_OK


def cmd_summarize_submit_plan(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.submit_plan_summary."""
    from hpc_agent.atoms.submit_plan_summary import summarize_submit_plan

    spec = _load_spec(args.spec, schema_name=None)
    if not isinstance(spec, dict):
        return _err(
            error_code="spec_invalid",
            message="summarize-submit-plan input must be a JSON object",
            category="user",
            retry_safe=False,
        )
    _ok(summarize_submit_plan(spec), name="summarize-submit-plan")
    return EXIT_OK


def cmd_verify_aggregation_complete(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.aggregation_invariants."""
    from hpc_agent.atoms.aggregation_invariants import verify_aggregation_complete

    _ok(
        verify_aggregation_complete(
            args.experiment_dir,
            run_id=args.run_id,
            combiner_dir_local=args.combiner_dir,
        ),
        name="verify-aggregation-complete",
    )
    return EXIT_OK


def cmd_verify_canary(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.canary_verify."""
    from hpc_agent.atoms.canary_verify import verify_canary

    _ok(
        verify_canary(
            args.experiment_dir,
            canary_run_id=args.canary_run_id,
            expect_output=args.expect_output,
            poll_interval_sec=int(args.poll_interval_sec),
            wait_budget_sec=int(args.wait_budget_sec),
        ),
        name="verify-canary",
    )
    return EXIT_OK


def cmd_cluster_reduce(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.cluster_reduce."""
    from hpc_agent.atoms.cluster_reduce import cluster_reduce

    extra_env: dict[str, str] | None = None
    if getattr(args, "extra_env", None):
        extra_env = {}
        for tok in str(args.extra_env).split(","):
            if "=" in tok:
                k, _, v = tok.partition("=")
                extra_env[k.strip()] = v.strip()
    out = cluster_reduce(
        args.experiment_dir,
        run_id=args.run_id,
        aggregate_cmd=args.aggregate_cmd,
        output_path=args.output_path,
        local_dir=args.local_dir,
        extra_env=extra_env,
        timeout_sec=int(args.timeout_sec),
    )
    _ok(out, name="cluster-reduce")
    return EXIT_OK


def cmd_axes_init(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.axes_init."""
    from hpc_agent.atoms.axes_init import axes_init

    homogeneous = (
        [s.strip() for s in args.homogeneous_axes.split(",") if s.strip()]
        if args.homogeneous_axes
        else []
    )
    axes_list: list[dict[str, object]] = []
    if args.axes:
        for tok in args.axes.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" not in tok:
                raise errors.SpecInvalid(f"--axes entry {tok!r} must be NAME:SIZE")
            name, _, size_s = tok.partition(":")
            try:
                size = int(size_s)
            except ValueError as exc:
                raise errors.SpecInvalid(f"--axes entry {tok!r} has non-integer size") from exc
            axes_list.append({"name": name.strip(), "size": size})
    _ok(
        axes_init(
            experiment_dir=args.experiment_dir,
            axes=axes_list or None,
            homogeneous_axes=homogeneous,
            force=args.force,
        ),
        name="axes-init",
    )
    return EXIT_OK


def cmd_campaign_init(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_init."""
    from hpc_agent.atoms.campaign_init import campaign_init

    _ok(
        campaign_init(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            goal=args.goal,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
            strategy_name=args.strategy_name,
            strategy_params_json=args.strategy_params_json,
        ),
        name="campaign-init",
    )
    return EXIT_OK


def cmd_campaign_replay(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_replay."""
    from hpc_agent.atoms.campaign_replay import campaign_replay

    _ok(
        campaign_replay(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            last_n=args.last_n,
        ),
        name="campaign-replay",
    )
    return EXIT_OK


def cmd_campaign_converged(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_converged."""
    from hpc_agent.atoms.campaign_converged import campaign_converged

    _ok(
        campaign_converged(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
        ),
        name="campaign-converged",
    )
    return EXIT_OK


def cmd_campaign_budget(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_budget."""
    from hpc_agent.atoms.campaign_budget import campaign_budget

    _ok(
        campaign_budget(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
        ),
        name="campaign-budget",
    )
    return EXIT_OK


def cmd_campaign_advance(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.campaign_advance."""
    from hpc_agent.atoms.campaign_advance import campaign_advance

    _ok(
        campaign_advance(
            experiment_dir=args.experiment_dir,
            campaign_id=args.campaign_id,
            max_iters=args.max_iters,
            metric=args.metric,
            target=args.target,
            direction=args.direction,
            plateau_window=args.plateau_window,
            plateau_tolerance=args.plateau_tolerance,
            max_jobs=args.max_jobs,
            max_tasks=args.max_tasks,
            max_walltime_sec=args.max_walltime_sec,
        ),
        name="campaign-advance",
    )
    return EXIT_OK


# ─── subcommand: status ────────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for run_id {args.run_id!r} in {args.experiment_dir}"
        )
    updated = runner.record_status(
        args.experiment_dir,
        args.run_id,
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        job_ids=record.job_ids,
        job_name=record.job_name,
    )
    data: dict[str, Any] = {
        "run_id": updated.run_id,
        "lifecycle_state": updated.status,
        "last_status": updated.last_status,
        "last_status_age_seconds": _last_status_age_seconds(updated.last_status),
        "combined_waves": updated.combined_waves,
        "failed_waves": updated.failed_waves,
    }
    # Surface the campaign tag so a caller seeing /status output knows
    # this run is part of a closed-loop campaign without separately
    # querying `campaign list` / `campaign status`.
    if updated.campaign_id:
        data["campaign_id"] = updated.campaign_id

    # A-M1: surface preempted-task counts directly on /status so a
    # caller polling a partially-bumped run sees them without first
    # having to call /failures. The campus user's harness can branch
    # on "X of N tasks got preempted" while the run is still in
    # flight, instead of waiting for the whole array to fail before
    # noticing scheduler pressure. Sourced from the per-task sidecar
    # ``preempt`` block written by dispatch.py's SIGTERM handler.
    preempt_summary = _preempted_summary_from_sidecar(args.experiment_dir, args.run_id)
    if preempt_summary is not None:
        count, ids = preempt_summary
        data["preempted_count"] = count
        data["preempted_task_ids"] = ids

    _ok(data, name="poll-run-status")
    return EXIT_OK


def _preempted_summary_from_sidecar(
    experiment_dir: Any, run_id: str
) -> tuple[int, list[int]] | None:
    """Return (preempted_count, preempted_task_ids_sorted) or None.

    Walks the per-task ``tasks`` block of the run sidecar and collects
    every task_id whose entry carries a ``preempt`` block (set by
    dispatch.py's SIGTERM handler when the cluster bumps the campus
    user's low-priority job). Returns None when there are no preempted
    tasks or when the sidecar can't be read — callers should treat
    None as "no preempt info to surface", not an error.
    """
    try:
        from hpc_agent.state.runs import (
            read_run_sidecar as _read_sidecar_for_status,
        )

        sidecar = _read_sidecar_for_status(Path(experiment_dir), run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, dict):
        return None
    tasks_block = sidecar.get("tasks") or {}
    if not isinstance(tasks_block, dict):
        return None
    preempted_ids: list[int] = []
    for tid_str, entry in tasks_block.items():
        if not isinstance(entry, dict):
            continue
        if "preempt" in entry:
            try:
                preempted_ids.append(int(tid_str))
            except (TypeError, ValueError):
                continue
    if not preempted_ids:
        return None
    return len(preempted_ids), sorted(preempted_ids)


# ─── subcommand: submit ────────────────────────────────────────────────────


def cmd_submit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(spec, "submit")
    required = (
        "profile",
        "cluster",
        "ssh_target",
        "remote_path",
        "job_name",
        "run_id",
        "job_ids",
        "total_tasks",
    )
    missing = [k for k in required if k not in spec]
    if missing:
        raise errors.SpecInvalid(
            f"--spec missing required fields: {missing}. See docs/reference/cli-spec.md."
        )

    if args.dry_run:
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "dry_run": True,
            },
            name="submit-spec",
        )
        return EXIT_OK

    from hpc_agent._schema_models.actions.submit import SubmitSpec as _SubmitSpec

    record, deduped = runner.submit_and_record(
        args.experiment_dir,
        spec=_SubmitSpec.model_validate(spec),
    )
    _ok(
        {
            "run_id": record.run_id,
            "job_ids": record.job_ids,
            "total_tasks": record.total_tasks,
            "deduped": deduped,
        },
        name="submit-spec",  # honest now that submit_and_record dedups
    )
    return EXIT_OK


# ─── subcommand: submit-flow ───────────────────────────────────────────────


def cmd_submit_flow(args: argparse.Namespace) -> int:
    """Workflow atom — pre-flight + rsync + deploy + qsub + record in one shot.

    See ``hpc_agent/job/submit_flow.py`` for the pipeline contract
    and ``schemas/submit_flow.{input,output}.json`` for the envelope
    shapes. Idempotent on ``run_id`` via the same dedup mechanism as
    ``submit``.

    **Auto-dispatch**: if the loaded spec is a batch shape (an object
    with a ``specs`` list, matching ``submit_flow_batch.input.json``)
    this subcommand transparently routes to
    :func:`cmd_submit_flow_batch`. Single-spec callers see no change;
    multi-spec callers don't have to know about a separate CLI.
    """
    spec = _load_spec(args.spec, schema_name=None)
    # Auto-dispatch: any shape that the batch CLI accepts (an object
    # with a `specs` list) routes there, bypassing the per-spec path.
    # Lets the slash command always say "call submit-flow" and stay
    # right whether the iteration emits 1 spec or N.
    if isinstance(spec, dict) and isinstance(spec.get("specs"), list):
        return cmd_submit_flow_batch(args)

    from hpc_agent.flows.submit_flow import submit_flow

    # Surface --partial-ok at the CLI in addition to spec.partial_ok so a
    # caller can opt in via either path. Flag wins over spec when both
    # are set (CLI is the more explicit override).
    if getattr(args, "partial_ok", False):
        spec = dict(spec)
        spec["partial_ok"] = True
    _validate_against_schema(spec, "submit_flow")

    if args.dry_run:
        _ok(
            {
                "would_launch": int(spec["total_tasks"]),
                "profile": spec["profile"],
                "cluster": spec["cluster"],
                "run_id": spec["run_id"],
                "canary": bool(spec.get("canary", True)),
                "dry_run": True,
            },
            name="submit-flow",
        )
        return EXIT_OK

    from hpc_agent._schema_models.workflows.submit_flow import SubmitFlowSpec

    submit_spec = SubmitFlowSpec.model_validate(spec)
    result = submit_flow(args.experiment_dir, spec=submit_spec)
    _ok(result.to_envelope_data(), name="submit-flow")
    return EXIT_OK


# ─── subcommand: submit-flow-batch ─────────────────────────────────────────


def cmd_submit_flow_batch(args: argparse.Namespace) -> int:
    """Workflow atom — submit N specs sharing one (ssh_target, remote_path).

    The bundle does ONE rsync_push + ONE deploy_runtime + N × (qsub +
    record), reusing the ssh ControlMaster across qsubs. This is the
    correct shape for campaign-time fan-out (e.g. 5 lgbm-tune
    submissions sharing one cluster) — the per-spec submit_flow path
    fired N × 13 ssh handshakes which tripped MaxStartups on CARC.

    Spec file is a JSON list of submit-flow specs (each matching
    ``schemas/submit_flow.input.json``); all entries MUST share
    ssh_target and remote_path. The CLI emits one envelope wrapping
    a list of per-spec result records.
    """
    from hpc_agent._schema_models.workflows.submit_flow_batch import SubmitFlowBatchSpec
    from hpc_agent.flows.submit_flow import submit_flow_batch

    raw = _load_spec(args.spec, schema_name=None)
    # Wrapper-shape validation (object with `specs` array, per-entry
    # required keys via submit_flow_batch.input.json), then full per-entry
    # validation against submit_flow.input.json. The two schemas overlap
    # on the required-keys check; the wrapper exists so an agent / external
    # orchestrator can sanity-check the bundle in one call.
    _validate_against_schema(raw, "submit_flow_batch")
    if not isinstance(raw, dict) or "specs" not in raw:
        return _err(
            error_code="spec_invalid",
            message="submit-flow-batch spec must be an object with a 'specs' list",
            category="user",
            retry_safe=False,
        )
    for entry in raw["specs"]:
        _validate_against_schema(entry, "submit_flow")
    batch_spec = SubmitFlowBatchSpec.model_validate(raw)

    if args.dry_run:
        targets = sorted({(s.ssh_target, s.remote_path) for s in batch_spec.specs})
        _ok(
            {
                "would_launch": [
                    {"run_id": s.run_id, "tasks": s.total_tasks} for s in batch_spec.specs
                ],
                "shared_targets": [{"ssh_target": t[0], "remote_path": t[1]} for t in targets],
                "n_specs": len(batch_spec.specs),
                "dry_run": True,
            },
            name="submit-flow-batch",
        )
        return EXIT_OK

    results = submit_flow_batch(args.experiment_dir, spec=batch_spec)
    _ok(
        {"results": [r.to_envelope_data() for r in results], "n_results": len(results)},
        name="submit-flow-batch",
    )
    return EXIT_OK


# ─── subcommand: monitor-flow ──────────────────────────────────────────────


def cmd_monitor_flow(args: argparse.Namespace) -> int:
    """Workflow atom — poll a run to terminal-or-budget; auto-combine waves.

    See ``hpc_agent/job/monitor_flow.py`` for the loop contract and
    ``schemas/monitor_flow.{input,output}.json`` for the envelope shapes.
    Internal poll loop runs to terminal lifecycle, wall-clock budget,
    or escalation; emits one envelope at the end. Pairs with
    ``submit-flow`` for the campaign composition pattern
    ``submit-flow → monitor-flow → next iteration``.
    """
    from hpc_agent._schema_models.workflows.monitor_flow import MonitorFlowSpec
    from hpc_agent.flows.monitor_flow import monitor_flow

    raw = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(raw, "monitor_flow")
    # The Pydantic model is the authoring SoT for the wire shape; the
    # jsonschema check above is a belt-and-suspenders fail-fast.
    monitor_spec = MonitorFlowSpec.model_validate(raw)

    if args.dry_run:
        _ok(
            {
                "run_id": monitor_spec.run_id,
                "poll_interval_seconds": monitor_spec.poll_interval_seconds,
                "wall_clock_budget_seconds": monitor_spec.wall_clock_budget_seconds,
                "auto_combine_waves": monitor_spec.auto_combine_waves,
                "dry_run": True,
            },
            name="monitor-flow",
        )
        return EXIT_OK

    result = monitor_flow(args.experiment_dir, spec=monitor_spec)
    _ok(result.to_envelope_data(), name="monitor-flow")
    return EXIT_OK


# ─── subcommand: aggregate-flow ────────────────────────────────────────────


def cmd_aggregate_flow(args: argparse.Namespace) -> int:
    """Workflow atom — ensure all waves combined, pull partials, reduce locally.

    See ``hpc_agent/job/aggregate_flow.py`` for the pipeline contract
    and ``schemas/aggregate_flow.{input,output}.json`` for the envelope
    shapes. Pairs with submit-flow + monitor-flow as the third workflow
    atom — the campaign loop's per-iteration tail is
    ``submit-flow → monitor-flow → aggregate-flow → next iter``.
    """
    from hpc_agent._schema_models.workflows.aggregate_flow import AggregateFlowSpec
    from hpc_agent.flows.aggregate_flow import aggregate_flow

    raw = _load_spec(args.spec, schema_name=None)
    _validate_against_schema(raw, "aggregate_flow")
    aggregate_spec = AggregateFlowSpec.model_validate(raw)

    if args.dry_run:
        _ok(
            {
                "run_id": aggregate_spec.run_id,
                "ensure_all_combined": aggregate_spec.ensure_all_combined,
                "pull_summaries": aggregate_spec.pull_summaries,
                "output_dir": aggregate_spec.output_dir,
                "dry_run": True,
            },
            name="aggregate-flow",
        )
        return EXIT_OK

    result = aggregate_flow(args.experiment_dir, spec=aggregate_spec)
    _ok(result.to_envelope_data(), name="aggregate-flow")
    return EXIT_OK


# ─── subcommand: aggregate ─────────────────────────────────────────────────


# Re-exported from hpc_agent.atoms.failures for back-compat with the
# auto-retry resolver test suite, which imports the helper directly.
# back-compat: introduced 0.2.0 (atoms split). Remove in 0.4.0 — update
# tests/test_failures*.py to import from hpc_agent.atoms.failures.
from hpc_agent.atoms.failures import _resolve_auto_retry  # noqa: E402,F401


def _sidecar_aggregate_defaults(experiment_dir: Path, run_id: str) -> dict[str, str]:
    """Read ``aggregate_defaults.{require_outputs,expect_output}`` from the run sidecar.

    Returns an empty dict when the sidecar is missing, malformed, or has
    no ``aggregate_defaults`` block. Silent failure is intentional —
    config validity is enforced by ``/submit``, not the aggregate path.
    """
    try:
        from hpc_agent.state.runs import read_run_sidecar
    except ImportError:
        return {}
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    block = sidecar.get("aggregate_defaults") or {}
    if not isinstance(block, dict):
        return {}
    return {
        k: block[k] for k in ("require_outputs", "expect_output") if isinstance(block.get(k), str)
    }


def cmd_aggregate(args: argparse.Namespace) -> int:
    # The aggregation pipeline is driven by hpc_agent.runner.combine_wave
    # plus the user-supplied combiner script on the cluster. The CLI wraps it
    # with three optional, framework-agnostic guarantees:
    #   --require-outputs <template>  : every per-task output exists before
    #                                   the combiner runs (precondition)
    #   --expect-output <path>        : the combiner produced a parseable
    #                                   artifact at <path> (postcondition)
    #   provenance                    : metadata block in envelope.data and
    #                                   sidecar file when --expect-output set
    # Defaults for require/expect can be set per-run in the sidecar's
    # ``aggregate_defaults`` block, populated by /submit. CLI flags win.
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = session.load_run(args.experiment_dir, args.run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for run_id {args.run_id!r}")
    if args.wave is None:
        raise errors.SpecInvalid("aggregate requires --wave <int>")

    # Resolve aggregate flags: explicit CLI > sidecar aggregate_defaults > none.
    # ``getattr`` keeps in-process callers (tests, slash-command wrappers)
    # working even when they hand-build a Namespace without these keys.
    defaults = _sidecar_aggregate_defaults(args.experiment_dir, args.run_id)
    require_outputs = getattr(args, "require_outputs", None) or defaults.get("require_outputs")
    expect_output = getattr(args, "expect_output", None) or defaults.get("expect_output")

    # Precondition: every per-task output must exist before we combine.
    if require_outputs:
        missing = runner.verify_per_task_outputs(
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            run_id=args.run_id,
            wave=int(args.wave),
            template=require_outputs,
        )
        if missing:
            preview = missing[:10]
            ellipsis = "..." if len(missing) > 10 else ""
            return _err_from_hpc(
                errors.OutputsMissing(
                    f"{len(missing)} per-task output(s) missing for wave "
                    f"{args.wave}: {preview}{ellipsis}",
                )
            )

    ok, stdout, stderr = runner.combine_wave(
        args.experiment_dir,
        args.run_id,
        wave=int(args.wave),
        ssh_target=record.ssh_target,
        remote_path=record.remote_path,
        force=args.force,
    )
    if ok:
        # Postcondition: the combiner must have produced the declared file.
        if expect_output:
            artifact_ok, detail = runner.verify_combiner_artifact(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                expect_output=expect_output,
            )
            if not artifact_ok:
                return _err_from_hpc(
                    errors.CombinerFailed(
                        f"combiner returned 0 but expected output {expect_output!r} {detail}",
                    )
                )

        provenance = runner.build_provenance(record, wave=int(args.wave))
        sidecar_path: str | None = None
        if expect_output:
            try:
                sidecar_path = runner.write_remote_provenance(
                    ssh_target=record.ssh_target,
                    remote_path=record.remote_path,
                    expect_output=expect_output,
                    provenance=provenance,
                )
            except errors.RemoteCommandFailed:
                # Best-effort — envelope still carries provenance.
                sidecar_path = None

        data: dict[str, Any] = {
            "run_id": args.run_id,
            "wave": int(args.wave),
            "combined": True,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "provenance": provenance,
        }
        if sidecar_path is not None:
            data["provenance_sidecar"] = sidecar_path
        # NOTE: cmd_aggregate has its own envelope shape (run_id + wave +
        # combined + provenance + tails) distinct from the ``combine-wave``
        # primitive's output schema (which mandates output_dir for the
        # cluster-side caller). The validate_output bypass is intentional
        # here; a dedicated ``aggregate-cli`` schema would be the right
        # forward fix, but is out of scope for this audit pass.
        _ok(data, idempotent=True)
        return EXIT_OK
    # Combiner returned non-zero — surface as a typed error so the
    # envelope's ``ok`` field and the exit code stay in sync.  Tail of
    # stderr was already in the success payload; here we put it in the
    # human-readable message so the caller can grep it.
    return _err_from_hpc(
        errors.CombinerFailed(
            f"combiner returned non-zero for wave {args.wave}; stderr tail: {stderr[-500:]!r}"
        )
    )


# ─── subcommand: resubmit ──────────────────────────────────────────────────


# Canonical failure-category vocabulary. Must be the UNION of:
#   - the auto-classifier in hpc_agent.runner.cluster_failures_by_fingerprint
#     (gpu_oom, system_oom, walltime, node_failure, import_error,
#      file_not_found, permission_denied, disk_full, python_traceback)
#   - the human-supplied taxonomy here (segv, queue_stall, code_bug, unknown)
# A test in tests/test_resubmit_batching.py asserts the classifier never emits a
# category outside this set.
# B2: derived from the canonical FailureCategory StrEnum.
# Pre-B2 this was a literal frozenset that drifted from the classifier
# emissions in hpc_agent.runner; A4 landed the union as a literal,
# B2 makes the literal redundant by sourcing from the StrEnum so the
# drift class cannot recur. test_lifecycle.py asserts the cross-set
# invariants (classifier emissions ⊆ accepted ⊆ FailureCategory).
from hpc_agent._internal.lifecycle import FailureCategory as _FailureCategory  # noqa: E402

_VALID_RESUBMIT_CATEGORIES = frozenset({fc.value for fc in _FailureCategory})


def cmd_resubmit(args: argparse.Namespace) -> int:
    spec = _load_spec(args.spec, schema_name="resubmit")
    failed = spec.get("failed_task_ids")
    category = spec.get("category")
    if not isinstance(failed, list) or not failed:
        raise errors.SpecInvalid("--spec.failed_task_ids must be a non-empty list")
    if not isinstance(category, str):
        raise errors.SpecInvalid("--spec.category must be a string")
    # Belt-and-braces: schema validation also enforces this enum, but
    # ``_validate_against_schema`` is a no-op when ``jsonschema`` is not
    # installed.  Keep the local check so the seven-category contract
    # holds either way.
    if category not in _VALID_RESUBMIT_CATEGORIES:
        raise errors.SpecInvalid(
            f"--spec.category must be one of {sorted(_VALID_RESUBMIT_CATEGORIES)}; got {category!r}"
        )

    from hpc_agent.flows.resubmit_flow import resubmit_flow

    # Validate per-element so a bad index surfaces with the slot
    # information rather than a bare ``ValueError: invalid literal for
    # int()``.
    parsed_failed: list[int] = []
    for i, t in enumerate(failed):
        try:
            parsed_failed.append(int(t))
        except (TypeError, ValueError) as exc:
            raise errors.SpecInvalid(
                f"--spec.failed_task_ids[{i}]={t!r} is not an integer"
            ) from exc

    result = resubmit_flow(
        Path(args.experiment_dir),
        args.run_id,
        failed_task_ids=parsed_failed,
        category=category,
        overrides=spec.get("overrides"),
        new_job_ids=spec.get("new_job_ids"),
        request_id=spec.get("request_id"),
        submit_to_cluster=bool(spec.get("submit_to_cluster", False)),
        script=spec.get("script"),
        backend=spec.get("backend"),
        job_name=spec.get("job_name"),
        job_env=spec.get("job_env"),
    )
    _ok(
        result.to_envelope_data(),
        # Honest now that resubmit_failed dedups on request_id: a replay
        # with the same spec is a no-op, just like submit.
        name="resubmit-failed",
    )
    return EXIT_OK


# ─── subcommand: reconcile ─────────────────────────────────────────────────


def cmd_reconcile(args: argparse.Namespace) -> int:
    if (rc := _require_ssh_agent()) is not None:
        return rc
    record = runner.reconcile(
        args.experiment_dir,
        args.run_id,
        scheduler=args.scheduler,
    )
    _ok(
        {
            "run_id": record.run_id,
            "lifecycle_state": record.status,
            "combined_waves": record.combined_waves,
            "failed_waves": record.failed_waves,
            "last_status": record.last_status,
        },
        name="reconcile-journal",
    )
    return EXIT_OK


# ─── subcommand: logs ──────────────────────────────────────────────────────


def cmd_logs(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.logs.

    Two ways to select tasks:
      --task-id 7,12,42   explicit list
      --all-failed        re-poll status, fetch logs for failed tasks
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    from hpc_agent.atoms.logs import fetch_logs

    # Parse the user-facing comma-separated --task-id at the adapter
    # boundary; the atom takes a typed list[int].
    task_ids: list[int] | None = None
    if not getattr(args, "all_failed", False) and args.task_id:
        try:
            task_ids = [int(t.strip()) for t in args.task_id.split(",") if t.strip()]
        except ValueError as exc:
            raise errors.SpecInvalid(f"--task-id must be comma-separated integers: {exc}") from exc
        if not task_ids:
            raise errors.SpecInvalid("--task-id is empty")

    data = fetch_logs(
        experiment_dir=args.experiment_dir,
        run_id=args.run_id,
        task_ids=task_ids,
        all_failed=bool(getattr(args, "all_failed", False)),
        lines=int(getattr(args, "lines", 50) or 50),
    )
    _ok(data, name="logs")
    return EXIT_OK


# ─── subcommand: failures ──────────────────────────────────────────────────


def cmd_failures(args: argparse.Namespace) -> int:
    """Argparse adapter — primitive lives at hpc_agent.atoms.failures.

    Cluster failed tasks by stderr fingerprint so 40 failures with the
    same root cause show up as one cluster instead of 40 separate logs
    to read.
    """
    if (rc := _require_ssh_agent()) is not None:
        return rc

    from hpc_agent.atoms.failures import fetch_failures

    _ok(
        fetch_failures(
            experiment_dir=args.experiment_dir,
            run_id=args.run_id,
            lines=int(getattr(args, "lines", 30) or 30),
        ),
        name="failures",
    )
    return EXIT_OK


# ─── subcommand: campaign-health ───────────────────────────────────────────


def cmd_campaign_health(args: argparse.Namespace) -> int:
    """Aggregate run-history into a campaign-health payload (D2a).

    Thin CLI wrapper. The ``@primitive(name="campaign-health", ...)``
    decorator lives on ``hpc_agent.atoms.campaign_health.campaign_health``
    (the module-level implementation), matching the ``backed_by.python``
    pointer in ``docs/primitives/campaign-health.md``.
    """
    from hpc_agent.atoms.campaign_health import campaign_health

    payload: dict[str, Any] = {}
    if args.campaign_id is not None:
        payload["campaign_id"] = args.campaign_id
    if args.since_iso is not None:
        payload["since_iso"] = args.since_iso
    if args.profile is not None:
        payload["profile"] = args.profile
    if args.cluster is not None:
        payload["cluster"] = args.cluster
    _validate_against_schema(payload, "campaign_health")
    from hpc_agent._schema_models.queries.campaign_health import CampaignHealthSpec

    spec = CampaignHealthSpec.model_validate(payload)
    try:
        data = campaign_health(args.experiment_dir, spec=spec)
    except Exception as exc:  # noqa: BLE001 — last-resort error envelope
        return _err(
            error_code="internal",
            message=f"campaign_health failed: {exc}",
            category="internal",
            retry_safe=False,
        )
    _ok(data, name="campaign-health")
    return EXIT_OK


# ─── subcommand: build-executor ────────────────────────────────────────────


def cmd_build_executor(args: argparse.Namespace) -> int:
    from hpc_agent.atoms.build_executor import build_executor

    data = build_executor(
        output_dir=args.output_dir,
        name=args.name,
        type=args.type,
        force=args.force,
    )
    _ok(data, name="build-executor")
    return EXIT_OK


# ─── subcommand: build-template ────────────────────────────────────────────


def cmd_build_template(args: argparse.Namespace) -> int:
    from hpc_agent.atoms.build_template import build_template

    data = build_template(repo_dir=args.repo_dir, force=args.force)
    _ok(data, name="build-template")
    return EXIT_OK


# ─── parser ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpc-agent",
        description=(
            "Submit, track status of, and aggregate parameter-grid HPC experiments. "
            "Stdout is a single-line JSON envelope; stderr is JSON-per-line "
            "log records. See docs/reference/cli-spec.md for full schemas."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {hpc_agent.__version__}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # capabilities
    p_cap = sub.add_parser(
        "capabilities",
        help="Machine-readable feature flags: subcommands, schedulers, schema dirs.",
    )
    p_cap.add_argument(
        "--full",
        action="store_true",
        help=(
            "Emit a plain-text llms-full dump (catalog + every primitive doc + "
            "schemas + envelope + boundary contract + cli-spec). Exception to the "
            "stdout-is-JSON contract; intended for one-shot LLM context loading."
        ),
    )
    p_cap.set_defaults(func=cmd_capabilities)

    # hook-install
    p_hook = sub.add_parser(
        "hook-install",
        help=(
            "Install hpc-agent Stop hooks into the user-global "
            "~/.claude/settings.json so the agent is held to slash-command "
            "exit contracts (e.g. /monitor-hpc must emit an `armed:` line). "
            "Writes to the user-global settings file unless --settings "
            "overrides; there is no automatic project-scoped install path "
            "today — point --settings at .claude/settings.json inside a "
            "repo to install per-project."
        ),
    )
    p_hook.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the merge without writing to settings.json.",
    )
    p_hook.add_argument(
        "--settings",
        type=str,
        default=None,
        help=(
            "Override the target settings path. Defaults to "
            "~/.claude/settings.json (user-global). Pass "
            "<repo>/.claude/settings.json to scope the install to a "
            "single project instead."
        ),
    )
    p_hook.set_defaults(func=cmd_hook_install)

    # install-commands
    p_install = sub.add_parser(
        "install-commands",
        help=(
            "Copy the bundled slash commands and skills into "
            "~/.claude/commands/ and ~/.claude/skills/. The pip-install "
            "entry point — run once after `pip install hpc-agent` to wire "
            "the agent assets into Claude Code. Idempotent (overwrites in "
            "place). Pass --claude-dir to target a non-default config dir."
        ),
    )
    p_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which commands/skills would be copied without writing.",
    )
    p_install.add_argument(
        "--claude-dir",
        type=str,
        default=None,
        help="Override the target Claude config dir. Defaults to ~/.claude.",
    )
    p_install.set_defaults(func=cmd_install_commands)

    # setup
    p_setup = sub.add_parser(
        "setup",
        help=(
            "One-shot setup: copy the bundled slash commands and skills "
            "into ~/.claude/ and install hpc-agent's Stop hooks. Run this "
            "once after `pip install hpc-agent`. Idempotent — safe to "
            "re-run. Use --no-hooks to skip the hook step."
        ),
    )
    p_setup.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview both steps without writing.",
    )
    p_setup.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip installing the Stop hooks (only copy commands + skills).",
    )
    p_setup.add_argument(
        "--claude-dir",
        type=str,
        default=None,
        help="Override the target Claude config dir. Defaults to ~/.claude.",
    )
    p_setup.set_defaults(func=cmd_setup)

    # axes-init
    p_axes = sub.add_parser(
        "axes-init",
        help=(
            "Write <experiment>/.hpc/axes.yaml with per-axis homogeneity "
            "hints used by the cold-start axis_picker. The agent typically "
            "calls this once per repo at deploy time after introspecting "
            "tasks.py."
        ),
    )
    _add_experiment_dir(p_axes)
    p_axes.add_argument(
        "--axes",
        type=str,
        default="",
        help=(
            "Comma-separated NAME:SIZE pairs for every parallel axis "
            "(e.g. 'model:4,data:3,window:20'). Order defines the "
            "cartesian-product convention; required for submit-flow's "
            "wave_map building."
        ),
    )
    p_axes.add_argument(
        "--homogeneous-axes",
        type=str,
        default="",
        help="Comma-separated axis names to mark homogeneous (e.g. 'window,fold').",
    )
    p_axes.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing axes.yaml. Default is refuse-without-force.",
    )
    p_axes.set_defaults(func=cmd_axes_init)

    # build-submit-spec
    p_bss = sub.add_parser(
        "build-submit-spec",
        help=(
            "Assemble + validate a submit_flow.input.json spec from "
            "resolved interview values (profile/cluster/ssh_target/.../"
            "cmd_sha/total_tasks). Emits the spec on stdout. Slash "
            "commands pipe the output straight into 'submit-flow --spec'."
        ),
    )
    _add_spec_and_dry_run(
        p_bss,
        schema_hint="JSON object of resolved interview kwargs (see build_submit_spec docstring)",
        dry_run_help="Build + validate the spec but don't emit (smoke check).",
    )
    p_bss.set_defaults(func=cmd_build_submit_spec)

    # build-tasks-py
    p_btp = sub.add_parser(
        "build-tasks-py",
        help=(
            "Scaffold <experiment>/.hpc/tasks.py from the canonical "
            "cartesian-product template (Pattern 1 of tasks_example.py). "
            "Spec file is {axes: [{name, values}], flags_by_executor: "
            "{module_path: [{name, type, default?}]}}. Refuses to "
            "overwrite an existing tasks.py without --force so "
            "hand-edited Pattern 2/3 conversions survive."
        ),
    )
    _add_experiment_dir(p_btp)
    p_btp.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .hpc/tasks.py.",
    )
    _add_spec_and_dry_run(
        p_btp,
        schema_hint=(
            "{axes: [{name, values}], flags_by_executor: {module: [{name, type, default?}]}}"
        ),
        dry_run_help="Validate the spec but don't write tasks.py.",
    )
    p_btp.set_defaults(func=cmd_build_tasks_py)

    # cluster-reduce
    p_cr = sub.add_parser(
        "cluster-reduce",
        help=(
            "Run the user's reducer on the cluster, pull only its single "
            "output JSON. Eliminates the bulk per-task rsync_pull failure "
            "mode at /aggregate-hpc + campaign-loop time."
        ),
    )
    _add_experiment_dir(p_cr)
    p_cr.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Run identifier (matches .hpc/runs/<run_id>.json).",
    )
    p_cr.add_argument(
        "--aggregate-cmd",
        type=str,
        default=None,
        help=(
            "Shell command to run on the cluster. Defaults to the run "
            "sidecar's aggregate_defaults.aggregate_cmd."
        ),
    )
    p_cr.add_argument(
        "--output-path",
        type=str,
        default=None,
        help=(
            "Cluster-side path the reducer writes its single JSON output. "
            "Defaults to '_aggregated/<run_id>.json' under remote_path. "
            "Threaded as $HPC_AGGREGATED_OUTPUT to the reducer."
        ),
    )
    p_cr.add_argument(
        "--local-dir",
        type=str,
        default=None,
        help="Local destination dir; defaults to <experiment>/_aggregated/<run_id>/.",
    )
    p_cr.add_argument(
        "--extra-env",
        type=str,
        default="",
        help=(
            "Comma-separated KEY=VALUE pairs forwarded to the reducer "
            "(in addition to HPC_RUN_ID / HPC_AGGREGATED_OUTPUT)."
        ),
    )
    p_cr.add_argument(
        "--timeout-sec",
        type=int,
        default=1800,
        help="Reducer timeout in seconds (default 1800 = 30 min).",
    )
    p_cr.set_defaults(func=cmd_cluster_reduce)

    # suggest-setup-action
    p_ssa = sub.add_parser(
        "suggest-setup-action",
        help=(
            "Run the /submit-hpc Setup priority cascade and recommend "
            "{action: monitor|reuse|interview|fresh, run_id, candidates}. "
            "Replaces the priority-list-walking prose at Step 0."
        ),
    )
    _add_experiment_dir(p_ssa)
    p_ssa.set_defaults(func=cmd_suggest_setup_action)

    # find-prior-run
    p_fpr = sub.add_parser(
        "find-prior-run",
        help=(
            "Look up a prior run by cmd_sha for /submit-hpc Step 6c "
            "resume detection. Returns {found, run_id, is_orphan, "
            "status, age_sec, ...}."
        ),
    )
    _add_experiment_dir(p_fpr)
    p_fpr.add_argument(
        "--cmd-sha",
        type=str,
        required=True,
        help="The cmd_sha (SHA-256 hex) to match against existing sidecars.",
    )
    p_fpr.set_defaults(func=cmd_find_prior_run)

    # summarize-submit-plan
    p_ssp = sub.add_parser(
        "summarize-submit-plan",
        help=(
            "Render the canonical pre-submit confirmation summary for a "
            "submit_flow.input.json spec. Returns {headline, body, "
            "confirm_prompt} the slash command prints verbatim. "
            "Eliminates per-submit wording drift."
        ),
    )
    _add_spec_and_dry_run(
        p_ssp,
        schema_hint="schemas/submit_flow.input.json",
        dry_run_help="Validate but don't emit (the primitive has no side effects).",
    )
    p_ssp.set_defaults(func=cmd_summarize_submit_plan)

    # verify-aggregation-complete
    p_vac = sub.add_parser(
        "verify-aggregation-complete",
        help=(
            "Walk the run sidecar's wave_map + the locally-pulled "
            "_combiner/ dir; report all_waves_combined / all_tasks_present "
            "/ provenance_present invariants. Returns ok plus the missing "
            "/ unexpected lists."
        ),
    )
    _add_experiment_dir(p_vac)
    p_vac.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Run identifier (matches .hpc/runs/<run_id>.json sidecar stem).",
    )
    p_vac.add_argument(
        "--combiner-dir",
        type=Path,
        required=True,
        help="Local path the cluster's _combiner/ was rsync_pull'd to.",
    )
    p_vac.set_defaults(func=cmd_verify_aggregation_complete)

    # verify-canary
    p_vc = sub.add_parser(
        "verify-canary",
        help=(
            "Wait + grep + output-check for a 1-task canary submission. "
            "Polls until terminal, scans stderr for known failure markers, "
            "optionally checks expect_output exists. Returns "
            "{ok, failure_kind, details, stderr_tail}."
        ),
    )
    _add_experiment_dir(p_vc)
    p_vc.add_argument(
        "--canary-run-id",
        type=str,
        required=True,
        help="Run ID of the canary (typically <main_run_id>-canary).",
    )
    p_vc.add_argument(
        "--expect-output",
        type=str,
        default=None,
        help="Optional path (relative to remote_path) the canary should have written.",
    )
    p_vc.add_argument(
        "--poll-interval-sec",
        type=int,
        default=30,
        help="Seconds between status polls (default 30).",
    )
    p_vc.add_argument(
        "--wait-budget-sec",
        type=int,
        default=1800,
        help="Total seconds to wait for terminal before giving up (default 1800).",
    )
    p_vc.set_defaults(func=cmd_verify_canary)

    # decide-monitor-arm
    p_dma = sub.add_parser(
        "decide-monitor-arm",
        help=(
            "Pick cron/loop/none + cadence + cron schedule string from "
            "the run's current summary. Returns the literal armed: line "
            "the slash command must emit (Stop hook checks for it) and "
            "ready-to-pass CronCreate args. Replaces /monitor-hpc Step 5 "
            "agent judgment."
        ),
    )
    _add_spec_and_dry_run(
        p_dma,
        schema_hint=(
            "{run_id, summary, total_tasks, invocation_argv, "
            "user_invoked_via_loop?, eta_sec?, pace_unstable?, queue_wait_sec?}"
        ),
        dry_run_help="Validate but don't emit (the primitive has no side effects anyway).",
    )
    p_dma.set_defaults(func=cmd_decide_monitor_arm)

    # monitor-summary
    p_ms = sub.add_parser(
        "monitor-summary",
        help=(
            "Render the canonical user-facing tick summary for a run. "
            "Reads .hpc/runs/<run_id>.monitor.jsonl + the run journal "
            "and returns {lifecycle_state, headline, body, armed_hint}. "
            "Slash command prints these verbatim."
        ),
    )
    _add_experiment_dir(p_ms)
    p_ms.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Run identifier (matches the .hpc/runs/<run_id>.json sidecar stem).",
    )
    p_ms.set_defaults(func=cmd_monitor_summary)

    # preflight
    p_pre = sub.add_parser(
        "preflight",
        help="Health check: SSH agent, ssh/rsync on PATH, clusters.yaml parses.",
    )
    p_pre.add_argument("--cluster", help="Optional cluster name to TCP-probe on :22.")
    p_pre.set_defaults(func=cmd_preflight)

    # validate-campaign
    p_vc = sub.add_parser(
        "validate-campaign",
        help=(
            "Pre-submit validator: cross-check tasks.py kwargs vs the executor "
            "signature, verify dataset row indices + non-null cols, and compare "
            "requested walltime against historical p95 + .hpc/playbook.yaml rules."
        ),
    )
    p_vc.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="Path to validate_campaign.input.json conforming to the schema.",
    )
    p_vc.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path("."),
        help="Path to the experiment directory; defaults to cwd.",
    )
    p_vc.set_defaults(func=cmd_validate_campaign)

    # interview
    p_iv = sub.add_parser(
        "interview",
        help=(
            "Validate an agent-written tasks.py against the structured intent "
            "from an interview, then persist intent + cmd_sha + dry-resolve "
            "preview to <campaign-dir>/interview.json."
        ),
    )
    p_iv.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="Path to interview.input.json conforming to schemas/interview.input.json.",
    )
    p_iv.add_argument(
        "--campaign-dir",
        required=True,
        help=(
            "Campaign workdir; must already contain a tasks.py written by the "
            "interview agent. interview.json (and optionally meta.json) is "
            "written into this directory."
        ),
    )
    p_iv.set_defaults(func=cmd_interview)

    # recall
    p_rc = sub.add_parser(
        "recall",
        help=(
            "Query past interview.json files under --root. Returns "
            "recency-sorted campaign summaries (goal, task_kind, "
            "task_count, operator, materialized_at, cmd_sha) for use as "
            "context in the next interview."
        ),
    )
    p_rc.add_argument(
        "--root",
        help=(
            "Filesystem directory to walk recursively for interview.json. "
            "When omitted, falls back to ~/.hpc-agent/config.json:"
            "experiment_roots; if neither is set, errors."
        ),
    )
    p_rc.add_argument(
        "--task-kind",
        help="Exact-match filter against intent.task_kind.",
    )
    p_rc.add_argument(
        "--operator",
        help="Exact-match filter against intent.produced_by.operator.",
    )
    p_rc.add_argument(
        "--since",
        help="ISO-8601 timestamp; only campaigns materialized at or after this point are returned.",
    )
    p_rc.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of summaries to return (default 20).",
    )
    p_rc.add_argument(
        "--include-runtime",
        action="store_true",
        help=(
            "Tier 2 rollup: walk each matched campaign's .hpc/runtimes/*.json "
            "and aggregate elapsed_sec / failure rate across all dispatched tasks."
        ),
    )
    p_rc.add_argument(
        "--include-generator-stats",
        action="store_true",
        help=(
            "Tier 3 rollup: bucket by task_generator.kind and report observed "
            "parameter envelopes. Most useful with --task-kind also set."
        ),
    )
    p_rc.set_defaults(func=cmd_recall)

    # discover
    p_disc = sub.add_parser(
        "discover",
        help="List executor scripts in --experiment-dir (CLIs with __main__).",
    )
    _add_experiment_dir(p_disc)
    p_disc.add_argument(
        "--search-dirs",
        type=str,
        default=None,
        help=(
            "Comma-separated subdirectory names to scan under "
            "--experiment-dir (e.g. 'scripts' or 'scripts,executors'). "
            "Default: 'executors,scripts,src' with a fallback to the "
            "experiment-dir root. Pass this when the caller knows its "
            "own layout convention — e.g. an integrator with a "
            "modules-only 'src/' should pass --search-dirs scripts."
        ),
    )
    p_disc.set_defaults(func=cmd_discover)

    # discover-reducers
    p_dr = sub.add_parser(
        "discover-reducers",
        help=(
            "List candidate reducer / aggregator scripts in --experiment-dir "
            "(matches by filename stem and top-level function names like "
            "aggregate / reduce / score). Use at /aggregate-hpc time to find "
            "an existing reducer instead of writing a fresh one."
        ),
    )
    _add_experiment_dir(p_dr)
    p_dr.set_defaults(func=cmd_discover_reducers)

    # plan-throughput
    p_pt = sub.add_parser(
        "plan-throughput",
        help=(
            "Pack a task grid into batched submission waves. Pure-local: "
            "reads the cluster's constraints from clusters.yaml and returns "
            "the wave plan + wave_map for the per-run sidecar."
        ),
    )
    p_pt.add_argument(
        "--cluster",
        required=True,
        help="Cluster name; its constraints block in clusters.yaml supplies the limits.",
    )
    p_pt.add_argument(
        "--total-tasks",
        type=int,
        required=True,
        help="Total task count to pack into waves.",
    )
    p_pt.add_argument(
        "--est-task-duration-s",
        type=int,
        default=None,
        help=(
            "Estimated per-task wall seconds. When given, enables the "
            "walltime-feasibility check and the total-time estimate."
        ),
    )
    p_pt.set_defaults(func=cmd_plan_throughput)

    # clusters
    p_cl = sub.add_parser("clusters", help="Introspect available cluster definitions.")
    p_cl_sub = p_cl.add_subparsers(dest="clusters_cmd", required=True)
    p_cl_list = p_cl_sub.add_parser("list", help="List all clusters.")
    p_cl_list.set_defaults(func=cmd_clusters_list)
    p_cl_desc = p_cl_sub.add_parser("describe", help="Print one cluster's config.")
    p_cl_desc.add_argument("name")
    p_cl_desc.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Surface yaml keys not recognized by ClusterConfig under "
            "data.unknown_keys. Useful for catching typos that the "
            "default extra='ignore' validation would silently drop."
        ),
    )
    p_cl_desc.set_defaults(func=cmd_clusters_describe)

    # list-in-flight
    p_lif = sub.add_parser(
        "list-in-flight",
        help="List runs with status=in_flight in the journal (recovery path).",
    )
    _add_experiment_dir(p_lif)
    p_lif.set_defaults(func=cmd_list_in_flight)

    # campaign — closed-loop campaign read-only commands
    p_camp = sub.add_parser(
        "campaign",
        help="Closed-loop campaign read-only commands (status, list).",
    )
    p_camp_sub = p_camp.add_subparsers(dest="action", required=True)

    p_camp_st = p_camp_sub.add_parser(
        "status",
        help=(
            "Report per-iteration reduced metrics for one campaign. "
            "Walks every sidecar tagged with --campaign-id, runs "
            "reduce_metrics on each, and emits the history dict-list."
        ),
    )
    _add_experiment_dir(p_camp_st)
    p_camp_st.add_argument("--campaign-id", required=True)
    p_camp_st.set_defaults(func=cmd_campaign_status)

    p_camp_ls = p_camp_sub.add_parser(
        "list",
        help="List every campaign with at least one sidecar in this experiment.",
    )
    _add_experiment_dir(p_camp_ls)
    p_camp_ls.set_defaults(func=cmd_campaign_list)

    p_camp_in = p_camp_sub.add_parser(
        "init",
        help="Write the campaign manifest from CLI args.",
    )
    _add_experiment_dir(p_camp_in)
    p_camp_in.add_argument("--campaign-id", required=True)
    p_camp_in.add_argument("--goal", type=str, default="")
    p_camp_in.add_argument("--max-iters", type=int, default=None)
    p_camp_in.add_argument("--metric", type=str, default=None)
    p_camp_in.add_argument("--target", type=float, default=None)
    p_camp_in.add_argument("--direction", choices=["minimize", "maximize"], default=None)
    p_camp_in.add_argument("--plateau-window", type=int, default=None)
    p_camp_in.add_argument("--plateau-tolerance", type=float, default=None)
    p_camp_in.add_argument("--max-jobs", type=int, default=None)
    p_camp_in.add_argument("--max-tasks", type=int, default=None)
    p_camp_in.add_argument("--max-walltime-sec", type=int, default=None)
    p_camp_in.add_argument("--strategy-name", type=str, default=None)
    p_camp_in.add_argument(
        "--strategy-params-json",
        type=str,
        default=None,
        help="JSON object for strategy.params (round-tripped untouched).",
    )
    p_camp_in.set_defaults(func=cmd_campaign_init)

    p_camp_rp = p_camp_sub.add_parser(
        "replay",
        help="Return the last N iterations of a campaign with reduced metrics.",
    )
    _add_experiment_dir(p_camp_rp)
    p_camp_rp.add_argument("--campaign-id", required=True)
    p_camp_rp.add_argument("--last-n", type=int, default=5)
    p_camp_rp.set_defaults(func=cmd_campaign_replay)

    p_camp_cv = p_camp_sub.add_parser(
        "converged",
        help="Apply user-supplied stop criteria to a campaign's history.",
    )
    _add_experiment_dir(p_camp_cv)
    p_camp_cv.add_argument("--campaign-id", required=True)
    p_camp_cv.add_argument("--max-iters", type=int, default=None)
    p_camp_cv.add_argument("--metric", type=str, default=None)
    p_camp_cv.add_argument("--target", type=float, default=None)
    p_camp_cv.add_argument("--direction", choices=["minimize", "maximize"], default=None)
    p_camp_cv.add_argument("--plateau-window", type=int, default=None)
    p_camp_cv.add_argument("--plateau-tolerance", type=float, default=None)
    p_camp_cv.set_defaults(func=cmd_campaign_converged)

    p_camp_bg = p_camp_sub.add_parser(
        "budget",
        help="Roll up campaign-level spend and compare to optional caps.",
    )
    _add_experiment_dir(p_camp_bg)
    p_camp_bg.add_argument("--campaign-id", required=True)
    p_camp_bg.add_argument("--max-jobs", type=int, default=None)
    p_camp_bg.add_argument("--max-tasks", type=int, default=None)
    p_camp_bg.add_argument("--max-walltime-sec", type=int, default=None)
    p_camp_bg.set_defaults(func=cmd_campaign_budget)

    p_camp_ad = p_camp_sub.add_parser(
        "advance",
        help=(
            "Decide the next campaign action (continue / stop_converged / "
            "stop_over_budget / wait_in_flight)."
        ),
    )
    _add_experiment_dir(p_camp_ad)
    p_camp_ad.add_argument("--campaign-id", required=True)
    p_camp_ad.add_argument("--max-iters", type=int, default=None)
    p_camp_ad.add_argument("--metric", type=str, default=None)
    p_camp_ad.add_argument("--target", type=float, default=None)
    p_camp_ad.add_argument("--direction", choices=["minimize", "maximize"], default=None)
    p_camp_ad.add_argument("--plateau-window", type=int, default=None)
    p_camp_ad.add_argument("--plateau-tolerance", type=float, default=None)
    p_camp_ad.add_argument("--max-jobs", type=int, default=None)
    p_camp_ad.add_argument("--max-tasks", type=int, default=None)
    p_camp_ad.add_argument("--max-walltime-sec", type=int, default=None)
    p_camp_ad.set_defaults(func=cmd_campaign_advance)

    # status
    p_st = sub.add_parser(
        "status", help="Poll cluster status for a run_id; one-shot, returns snapshot."
    )
    _add_experiment_dir(p_st)
    _add_run_id(p_st)
    p_st.set_defaults(func=cmd_status)

    # submit
    p_sub = sub.add_parser(
        "submit",
        help=(
            "Record a submission in the journal. Idempotent on run_id: "
            "the bundled atomic-ops layer dedups so a retry on transient "
            "network errors does not double-submit."
        ),
    )
    _add_experiment_dir(p_sub)
    p_sub.add_argument("--spec", type=Path, required=True, help="JSON spec file")
    p_sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the spec and report what would be launched; no SSH/qsub.",
    )
    p_sub.set_defaults(func=cmd_submit)

    # submit-flow
    p_sf = sub.add_parser(
        "submit-flow",
        help=(
            "Workflow atom: pre-flight + rsync + deploy + qsub + record in "
            "one shot. Auto-dispatches to submit-flow-batch when the spec "
            "is a {specs: [...]} object — callers always invoke this one "
            "subcommand whether the iteration emits 1 spec or N. Idempotent "
            "on run_id (or per-spec run_id when batched)."
        ),
    )
    p_sf.add_argument(
        "--partial-ok",
        action="store_true",
        help=(
            "Tolerate per-task failures: when the wave finishes, classify "
            "as `complete` if at least one task succeeded; record failed "
            "task IDs in <run_id>.failed.json so aggregate-flow can skip "
            "them. Without this flag (the default), any failure aborts the "
            "wave with lifecycle_state=failed."
        ),
    )
    _add_experiment_dir(p_sf)
    _add_spec_and_dry_run(
        p_sf,
        schema_hint="schemas/submit_flow.input.json",
        dry_run_help="Validate the spec and report what would be launched; no SSH/rsync/qsub.",
    )
    p_sf.set_defaults(func=cmd_submit_flow)

    # submit-flow-batch
    p_sfb = sub.add_parser(
        "submit-flow-batch",
        help=(
            "Workflow atom: rsync + deploy ONCE, then qsub N specs sharing "
            "the same (ssh_target, remote_path). Use whenever a campaign or "
            "sweep submits >1 specs to the same cluster — bundles 13×N ssh "
            "handshakes into ~3 (rsync + deploy + multiplexed qsubs). Spec "
            "file is a JSON list."
        ),
    )
    _add_experiment_dir(p_sfb)
    _add_spec_and_dry_run(
        p_sfb,
        schema_hint="schemas/submit_flow_batch.input.json (array of submit_flow.input.json items)",
        dry_run_help="Validate the batch + report shared targets; no SSH/rsync/qsub.",
    )
    p_sfb.set_defaults(func=cmd_submit_flow_batch)

    # monitor-flow
    p_mf = sub.add_parser(
        "monitor-flow",
        help=(
            "Workflow atom: poll a run to terminal lifecycle (or wall-clock "
            "budget); auto-combine waves as they finish; write the same "
            ".monitor.jsonl tick log /monitor-hpc writes. Pairs with "
            "submit-flow for the campaign loop composition. MVP does not "
            "auto-resubmit failed tasks."
        ),
    )
    _add_experiment_dir(p_mf)
    _add_spec_and_dry_run(
        p_mf,
        schema_hint="schemas/monitor_flow.input.json",
        dry_run_help="Validate the spec and report what would be polled; no SSH.",
    )
    p_mf.set_defaults(func=cmd_monitor_flow)

    # aggregate-flow
    p_af = sub.add_parser(
        "aggregate-flow",
        help=(
            "Workflow atom: ensure all waves combined on the cluster, "
            "rsync the _combiner/ partials locally, reduce_partials over "
            "them, optionally pull per-task summaries. Third atom in the "
            "submit-flow → monitor-flow → aggregate-flow campaign chain."
        ),
    )
    _add_experiment_dir(p_af)
    _add_spec_and_dry_run(
        p_af,
        schema_hint="schemas/aggregate_flow.input.json",
        dry_run_help="Validate the spec and report what would be aggregated; no SSH.",
    )
    p_af.set_defaults(func=cmd_aggregate_flow)

    # aggregate
    p_agg = sub.add_parser(
        "aggregate",
        help="Run the on-cluster combiner for one wave; records outcome to journal.",
    )
    _add_experiment_dir(p_agg)
    _add_run_id(p_agg)
    p_agg.add_argument("--wave", type=int, required=True)
    p_agg.add_argument(
        "--force",
        action="store_true",
        help="Re-run the combiner even if the wave appears combined.",
    )
    p_agg.add_argument(
        "--require-outputs",
        default=None,
        help=(
            "Path template (with {task_id}) checked on the cluster before "
            "the combiner runs. Refuses to combine if any task in this "
            "wave is missing its expected output. Default reads from the "
            "run sidecar's aggregate_defaults.require_outputs."
        ),
    )
    p_agg.add_argument(
        "--expect-output",
        default=None,
        help=(
            "Remote path (relative to remote_path) that the combiner must "
            "produce. Verified after the combiner exits 0; .json files "
            "are also checked for parseability. Default reads from the "
            "run sidecar's aggregate_defaults.expect_output."
        ),
    )
    p_agg.set_defaults(func=cmd_aggregate)

    # resubmit
    p_rs = sub.add_parser(
        "resubmit",
        help="Record a resubmission attempt in the journal (caller does the actual qsub).",
    )
    _add_experiment_dir(p_rs)
    _add_run_id(p_rs)
    p_rs.add_argument("--spec", type=Path, required=True)
    p_rs.set_defaults(func=cmd_resubmit)

    # reconcile
    p_rec = sub.add_parser(
        "reconcile",
        help="Re-derive ground truth from the cluster (status, waves, alive jobs).",
    )
    _add_experiment_dir(p_rec)
    _add_run_id(p_rec)
    p_rec.add_argument(
        "--scheduler",
        required=True,
        choices=["sge", "slurm"],
        help="Scheduler family — needed to query alive job IDs.",
    )
    p_rec.set_defaults(func=cmd_reconcile)

    # logs
    p_logs = sub.add_parser(
        "logs",
        help="Fetch per-task stderr logs from the cluster (requires --task-id or --all-failed).",
    )
    _add_experiment_dir(p_logs)
    _add_run_id(p_logs)
    p_logs.add_argument(
        "--task-id",
        default=None,
        help="Comma-separated task ids to fetch (e.g. '7,12,42').",
    )
    p_logs.add_argument(
        "--all-failed",
        action="store_true",
        help="Re-poll status and fetch logs for every task with status=failed.",
    )
    p_logs.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Number of trailing lines to return per log (default 50).",
    )
    p_logs.set_defaults(func=cmd_logs)

    # failures
    p_fail = sub.add_parser(
        "failures",
        help="Cluster failed tasks by stderr fingerprint for triage.",
    )
    _add_experiment_dir(p_fail)
    _add_run_id(p_fail)
    p_fail.add_argument(
        "--lines",
        type=int,
        default=30,
        help="Per-task stderr tail length used for fingerprinting (default 30).",
    )
    p_fail.set_defaults(func=cmd_failures)

    # campaign-health (D2a)
    p_ch = sub.add_parser(
        "campaign-health",
        help=(
            "Structured run-history aggregation for an LLM agent. Returns "
            "walltime cliff rates, failure breakdown, GPU utilization, and "
            "a ready-to-feed-LLM suggested_prompt."
        ),
    )
    _add_experiment_dir(p_ch)
    p_ch.add_argument("--campaign-id", default=None)
    p_ch.add_argument("--since-iso", default=None)
    p_ch.add_argument("--profile", default=None)
    p_ch.add_argument("--cluster", default=None)
    p_ch.set_defaults(func=cmd_campaign_health)

    # build-executor
    p_be = sub.add_parser(
        "build-executor",
        help="Scaffold a new executor from a starter template.",
    )
    p_be.add_argument("--name", required=True, help="Output filename stem (no .py).")
    p_be.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="Where to write the new file (default: CWD).",
    )
    p_be.add_argument(
        "--type",
        default="plain",
        choices=["plain"],
        help=(
            "Which template to instantiate. The only template is 'plain' "
            "(a standard executor scaffold); per-task fan-out lives "
            "inline in .hpc/tasks.py, scaffolded by /submit Step 6."
        ),
    )
    p_be.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the destination file if it already exists.",
    )
    p_be.set_defaults(func=cmd_build_executor)

    # build-template
    p_bt = sub.add_parser(
        "build-template",
        help="Inject the experiment-template scaffold into a repo.",
    )
    p_bt.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="Target repository root (default: CWD).",
    )
    p_bt.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite repo-root files that already exist. The "
            "framework-owned .hpc/ assets are re-injected regardless."
        ),
    )
    p_bt.set_defaults(func=cmd_build_template)

    # Optional plugin distributions add their own subcommands here. With
    # none installed this is a no-op and the parser is unchanged.
    from hpc_agent._internal.plugins import register_plugin_cli

    register_plugin_cli(sub)

    return parser


# ─── Verb grouping (post-rename UX bump) ───────────────────────────────────
#
# ``hpc-agent`` flat-lists 60-odd subcommands; agents that don't already
# know the surface struggle to discover the right one. Adding ``git
# remote`` / ``kubectl get``-style verb groups gives a navigable
# top-level. We don't want to refactor the entire argparse tree, so the
# grouping is implemented as an argv pre-processor: ``hpc-agent build
# build-executor <args>`` strips the ``build`` prefix before
# argparse sees it. The flat form (``hpc-agent build-executor
# <args>``) keeps working — both routes hit the same handler.
#
# Add a primitive to a group: append it to the matching frozenset.
# ``hpc-agent build`` (no further argv) prints the group's
# subcommand list.

_VERB_GROUPS: dict[str, frozenset[str]] = {
    # Only include subcommands that have parsers registered in
    # build_parser() — _strip_verb_group passes the verb through to
    # argparse, which raises "invalid choice" for unregistered names.
    "validate": frozenset(
        {
            "validate-campaign",
        }
    ),
    "build": frozenset(
        {
            "axes-init",
            "build-executor",
            "build-submit-spec",
            "build-tasks-py",
            "build-template",
        }
    ),
}


def _print_group_help(group: str) -> None:
    """List the subcommands belonging to a verb group, one per line."""
    members = sorted(_VERB_GROUPS[group])
    print(f"hpc-agent {group} <subcommand>", file=sys.stderr)
    print(f"\nSubcommands ({len(members)}):", file=sys.stderr)
    for cmd in members:
        print(f"  hpc-agent {group} {cmd}", file=sys.stderr)
    print(
        "\nFlat form also works: ``hpc-agent <subcommand>``. "
        "Pass ``--help`` to any subcommand for arguments.",
        file=sys.stderr,
    )


def _strip_verb_group(argv: list[str]) -> list[str]:
    """If argv[0] names a verb group, strip it (or print group help)."""
    if not argv or argv[0] not in _VERB_GROUPS:
        return argv
    group = argv[0]
    if len(argv) == 1 or argv[1] in {"-h", "--help"}:
        _print_group_help(group)
        raise SystemExit(0)
    if argv[1] in _VERB_GROUPS[group]:
        return argv[1:]
    # Unknown subcommand under a known group — surface a helpful error.
    print(
        f"hpc-agent: {argv[1]!r} is not in the {group!r} group.",
        file=sys.stderr,
    )
    _print_group_help(group)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252) whose codec
    # cannot encode the ``→`` and box-drawing characters in our --help
    # text and catalog tables, raising UnicodeEncodeError on print_help().
    # Force UTF-8 on the std streams up front. ``reconfigure`` exists on
    # io.TextIOWrapper (CPython 3.7+); guard for exotic stream
    # replacements (pytest capture, pipes) that lack it.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                _reconfigure(encoding="utf-8")

    # Populate the primitive registry once before any subcommand
    # dispatch — without this, get_registry() raises RuntimeError
    # (the previous auto-import path silently swallowed ImportError
    # and made missing-decorator bugs hard to diagnose).
    from hpc_agent._internal.primitive import register_primitives

    register_primitives()
    if argv is None:
        argv = sys.argv[1:]
    argv = _strip_verb_group(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc: int = args.func(args)
        return rc
    except errors.HpcError as exc:
        return _err_from_hpc(exc)
    except subprocess.TimeoutExpired as exc:
        # Backend ``qsub``/``sbatch``/``sacct`` exceeded its timeout.
        # Surface as a typed cluster-category error so callers know
        # to retry rather than treat it as user input invalid.
        return _err_from_hpc(
            errors.ClusterTimeout(
                f"scheduler subprocess timed out after {exc.timeout}s: {exc.cmd!r}"
            )
        )
    except ValueError as exc:
        # Route through the canonical errors enum rather than inlining
        # the "spec_invalid" string — keeps error_code values centralised
        # in slash_commands.errors.
        return _err_from_hpc(errors.SpecInvalid(str(exc)))
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        # The base HpcError carries error_code="internal" + category=
        # "internal" by default, which matches the previous inline
        # values. Wrap so callers see a uniform shape.
        return _err_from_hpc(errors.HpcError(f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    sys.exit(main())
