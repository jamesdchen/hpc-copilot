"""Integration tests for ``hpc_preamble.sh``.

Sources the preamble in a real bash subprocess against a fake
``$HPC_NFS_DATA_DIR`` so the campus user's NFS-staging path actually
runs end-to-end. Cheap and catches a future contributor moving the
staging block before ``set -e`` is in effect, or breaking the
md5-suffixed ``$LOCAL_DATA_DIR`` disambiguator that lets two
concurrent campaigns survive on the same node.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent import _PACKAGE_ROOT

if TYPE_CHECKING:
    from pathlib import Path

PREAMBLE = (
    _PACKAGE_ROOT / "models" / "mapreduce" / "templates" / "runtime" / "common" / "hpc_preamble.sh"
)

_BASH = shutil.which("bash")
_RSYNC = shutil.which("rsync")
_MD5SUM = shutil.which("md5sum")

pytestmark = pytest.mark.skipif(
    not (_BASH and _MD5SUM),
    reason="bash + md5sum required for preamble integration tests",
)
needs_rsync = pytest.mark.skipif(not _RSYNC, reason="rsync required for staging integration test")


def _make_rsync_stub(tmp_path: Path) -> Path:
    """Create a fake rsync stub on disk that copies src→dst with cp -a.

    Lets tests exercise the preamble's flock+rsync+set-e flow without
    requiring the system rsync binary (which isn't installed in some
    minimal CI containers). The stub matches the documented two-arg
    invocation: ``rsync -a SRC/ DST/``.
    """
    stub_dir = tmp_path / "stub_bin"
    stub_dir.mkdir(exist_ok=True)
    rsync = stub_dir / "rsync"
    rsync.write_text(
        textwrap.dedent(
            """\
            #!/bin/bash
            # Minimal rsync stub: rsync -a SRC/ DST/
            # Exit 1 on missing source so set-e tests still see a failure.
            for arg in "$@"; do
                case "$arg" in
                    -*) ;;
                    *) [ -z "${SRC:-}" ] && SRC="$arg" || DST="$arg" ;;
                esac
            done
            [ -d "$SRC" ] || exit 1
            mkdir -p "$DST"
            cp -a "$SRC"/. "$DST"/
            """
        )
    )
    rsync.chmod(0o755)
    return stub_dir


def _run_with_preamble(
    tmp_path: Path,
    *,
    nfs_data_dir: str,
    extra_setup: str = "",
    extra_assert: str = "echo OK",
    use_rsync_stub: bool = False,
) -> subprocess.CompletedProcess:
    """Source the preamble and run *extra_assert* afterwards.

    Stubs out ``module``/``conda``/``uv`` because the cluster-side
    binaries don't exist in CI. Sets HPC_RUNTIME to a no-op value so
    the ``uv sync`` block is skipped. When *use_rsync_stub* is set,
    prepends a fake rsync to PATH so tests run on hosts without it.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    tmpdir = tmp_path / "scratch"
    tmpdir.mkdir(exist_ok=True)

    path_prefix = ""
    if use_rsync_stub:
        stub_dir = _make_rsync_stub(tmp_path)
        path_prefix = f'export PATH="{stub_dir}:$PATH"\n'

    script = textwrap.dedent(
        f"""\
        set -e
        # Stub binaries used by other preamble blocks.
        module() {{ :; }}
        export -f module
        conda() {{ :; }}
        export -f conda

        {path_prefix}
        export REPO_DIR={repo_dir!s}
        export HPC_RUNTIME=none
        export TMPDIR={tmpdir!s}
        export HPC_NFS_DATA_DIR={nfs_data_dir!r}
        unset SLURM_TMPDIR
        {extra_setup}

        source {PREAMBLE!s}

        {extra_assert}
        """
    )
    return subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


def test_preamble_exports_local_data_dir_and_rsyncs(tmp_path: Path) -> None:
    """A fake $HPC_NFS_DATA_DIR is rsynced into $LOCAL_DATA_DIR and
    the campus user's executor can read from it via the documented
    ``$LOCAL_DATA_DIR`` contract."""
    src = tmp_path / "nfs_dataset"
    src.mkdir()
    (src / "marker.txt").write_text("hello")

    proc = _run_with_preamble(
        tmp_path,
        nfs_data_dir=str(src),
        use_rsync_stub=True,
        extra_assert=textwrap.dedent(
            """\
            test -n "$LOCAL_DATA_DIR" || exit 11
            test -f "$LOCAL_DATA_DIR/marker.txt" || exit 12
            test -f "$LOCAL_DATA_DIR/.staged_ok" || exit 13
            echo "$LOCAL_DATA_DIR"
            """
        ),
    )

    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    # The exported path should include the md5-suffixed hpc_agent_data tag.
    last_line = proc.stdout.strip().splitlines()[-1]
    assert "/hpc_agent_data_" in last_line, last_line


def test_preamble_set_e_propagates_rsync_failure(tmp_path: Path) -> None:
    """If rsync fails, the staging block must propagate failure to the
    parent (set -e doesn't cross subshell boundaries on its own; the
    preamble's `|| exit 2` is the bridge). Without this, the campus
    user's executor would silently run against missing data."""
    nonexistent = tmp_path / "definitely_not_a_real_path_xyz"

    proc = _run_with_preamble(
        tmp_path,
        nfs_data_dir=str(nonexistent),
        use_rsync_stub=True,
        extra_assert='echo "should not reach here"',
    )

    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "should not reach here" not in proc.stdout
    # Diagnostic line lands on stderr.
    assert "NFS staging" in proc.stderr or "rsync" in proc.stderr.lower()


def test_preamble_md5_disambiguates_two_datasets(tmp_path: Path) -> None:
    """Two concurrent campaigns with different $HPC_NFS_DATA_DIR must
    stage into distinct $LOCAL_DATA_DIR paths so they don't step on
    each other's data — without the md5 suffix, both would land at
    the same .../hpc_agent_data and silently corrupt each other."""
    src_a = tmp_path / "dataset_alpha"
    src_b = tmp_path / "dataset_beta"
    src_a.mkdir()
    src_b.mkdir()
    (src_a / "a.txt").write_text("alpha")
    (src_b / "b.txt").write_text("beta")

    proc_a = _run_with_preamble(
        tmp_path,
        nfs_data_dir=str(src_a),
        use_rsync_stub=True,
        extra_assert='echo "$LOCAL_DATA_DIR"',
    )
    proc_b = _run_with_preamble(
        tmp_path,
        nfs_data_dir=str(src_b),
        use_rsync_stub=True,
        extra_assert='echo "$LOCAL_DATA_DIR"',
    )
    assert proc_a.returncode == 0, proc_a.stderr
    assert proc_b.returncode == 0, proc_b.stderr

    path_a = proc_a.stdout.strip().splitlines()[-1]
    path_b = proc_b.stdout.strip().splitlines()[-1]
    assert path_a != path_b, f"two dataset paths collided: {path_a}"
    # Both should be in the same root, differing only by md5 suffix.
    assert path_a.rsplit("_", 1)[0] == path_b.rsplit("_", 1)[0]


def test_preamble_warns_on_tmp_fallback(tmp_path: Path) -> None:
    """When neither $SLURM_TMPDIR nor $TMPDIR is set, the preamble
    falls through to /tmp but emits a one-line warning so the campus
    user can diagnose a future quota-failed staging mid-run."""
    src = tmp_path / "nfs_dataset"
    src.mkdir()
    (src / "marker.txt").write_text("ok")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    stub_dir = _make_rsync_stub(tmp_path)

    script = textwrap.dedent(
        f"""\
        set -e
        module() {{ :; }}
        export -f module
        conda() {{ :; }}
        export -f conda
        export PATH="{stub_dir}:$PATH"
        export REPO_DIR={repo_dir!s}
        export HPC_RUNTIME=none
        export HPC_NFS_DATA_DIR={str(src)!r}
        unset SLURM_TMPDIR TMPDIR
        # /tmp must be writable for this test to work; if it isn't,
        # skipping is honest.
        test -w /tmp || exit 77

        source {PREAMBLE!s}

        echo "$LOCAL_DATA_DIR"
        """
    )
    proc = subprocess.run(
        [_BASH, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    if proc.returncode == 77:
        pytest.skip("/tmp not writable on this host")

    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "warning" in proc.stderr.lower()
    last_line = proc.stdout.strip().splitlines()[-1]
    try:
        assert last_line.startswith("/tmp/hpc_agent_data_"), last_line
    finally:
        # Don't leak the /tmp staging dir between test runs.
        if last_line.startswith("/tmp/hpc_agent_data_"):
            shutil.rmtree(last_line, ignore_errors=True)
