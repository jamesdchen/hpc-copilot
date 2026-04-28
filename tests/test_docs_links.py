"""Doc/code drift guard for the MARs proposal package.

The MARs-facing docs reference `error_code` values and env vars that
must stay in sync with the code. If a code change renames or drops one,
these tests fail before the proposal goes stale.
"""

from __future__ import annotations

import re
from pathlib import Path

from slash_commands import errors as errors_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
MARS_INTEGRATION = REPO_ROOT / "docs" / "mars-integration.md"
MARS_SNIPPET = REPO_ROOT / "docs" / "mars" / "experiment-runner.snippet.md"


def _all_error_codes() -> set[str]:
    """Every error_code defined on an HpcError subclass."""
    codes: set[str] = set()
    for name in dir(errors_mod):
        cls = getattr(errors_mod, name)
        if isinstance(cls, type) and issubclass(cls, errors_mod.HpcError):
            codes.add(cls.error_code)
    return codes


# error_code values appear in tables as `\`code\``. Match backtick-quoted
# snake_case tokens — the false-positive rate is low because every word
# we'd accidentally catch (e.g. `retry_safe`) is also documented.
_BACKTICK_TOKEN = re.compile(r"`([a-z][a-z0-9_]+)`")

# Known non-error-code identifiers that appear in backticks. Anything
# matched here is allowed without being an error_code.
_DOC_VOCABULARY = {
    # Subcommands
    "submit", "status", "aggregate", "reconcile", "resubmit", "preflight",
    "discover", "expand-grid", "list-in-flight", "clusters", "capabilities",
    "build-executor",
    # Envelope keys
    "ok", "data", "error_code", "category", "retry_safe", "remediation",
    "message", "idempotent",
    # Status fields
    "deduped", "lifecycle_state", "in_flight", "complete", "failed",
    "abandoned", "all_ok", "checks", "stderr_tail", "stdout_tail",
    "ssh_auth_sock", "cluster_tcp_22", "experiment_id", "manifest_sha",
    "run_id", "job_ids", "manifest_filename", "total_tasks", "profile",
    "ssh_target", "remote_path", "job_name", "wave", "seed",
    "timestamp", "models", "rankings", "statistical_tests",
    "qsub", "sbatch",
    "last_status",
    # Programs and runtimes
    "uv", "pip", "bash", "python", "python3",
    "scancel", "qdel",
    # Booleans / JSON literals
    "true", "false", "null",
    # Tier names
    "scripts", "src", "probe.py", "meta.json", "metrics.json",
    "results/metrics.json", "manifest.<sha8>.json",
    # Capabilities additions
    "mars_skill_paths", "required_env",
    # Categories
    "user", "cluster", "network", "internal",
}


def _doc_text(path: Path) -> str:
    return path.read_text()


def test_mars_integration_doc_exists() -> None:
    assert MARS_INTEGRATION.is_file(), MARS_INTEGRATION


def test_mars_snippet_exists() -> None:
    assert MARS_SNIPPET.is_file(), MARS_SNIPPET


def test_mars_integration_error_codes_match_code() -> None:
    """Every snake_case backtick token in the error-code table must be
    either a real error_code or a known doc-vocabulary term."""
    text = _doc_text(MARS_INTEGRATION)
    codes = _all_error_codes()

    seen_error_codes: set[str] = set()
    for token in _BACKTICK_TOKEN.findall(text):
        if token in codes:
            seen_error_codes.add(token)
        elif token in _DOC_VOCABULARY:
            continue
        else:
            # Unknown token — fail with a useful message.
            assert False, (
                f"docs/mars-integration.md mentions `{token}` in backticks, "
                f"which is neither an error_code nor in the known "
                f"vocabulary set. If it's a new error_code, add the class "
                f"to slash_commands/errors.py. If it's documentation "
                f"vocabulary, add it to _DOC_VOCABULARY in this test."
            )

    # Sanity check: at least the high-impact codes should be documented.
    must_document = {
        "ssh_unreachable",
        "scheduler_throttled",
        "manifest_invalid",
        "cluster_unknown",
    }
    missing = must_document - seen_error_codes
    assert not missing, (
        f"docs/mars-integration.md is missing error_code rows for: {missing}"
    )


def test_mars_snippet_error_codes_match_code() -> None:
    text = _doc_text(MARS_SNIPPET)
    codes = _all_error_codes()
    for token in _BACKTICK_TOKEN.findall(text):
        if token in codes or token in _DOC_VOCABULARY:
            continue
        assert False, (
            f"experiment-runner.snippet.md mentions `{token}` which is "
            f"neither an error_code nor in the known vocabulary."
        )


def test_mars_docs_env_vars_match_capabilities() -> None:
    """Env vars mentioned in docs must match capabilities.required_env."""
    from hpc_mapreduce.cli import _MARS_SKILL_NAMES  # noqa: F401  (just to ensure import)

    # Re-execute capabilities in-process to get the canonical list.
    import argparse
    import json
    from unittest.mock import patch

    captured: list[dict] = []

    def fake_emit(payload):
        captured.append(payload)

    from hpc_mapreduce import cli

    with patch.object(cli, "_emit", side_effect=fake_emit):
        cli.cmd_capabilities(argparse.Namespace())

    required_env = set(captured[-1]["data"]["required_env"])
    integration_text = _doc_text(MARS_INTEGRATION)
    for var in required_env:
        assert var in integration_text, (
            f"docs/mars-integration.md does not mention required env var {var!r}"
        )
