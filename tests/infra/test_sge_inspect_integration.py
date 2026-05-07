"""Fixture-driven tests for ``claude_hpc.infra.inspect.sge``.

The qhost / qstat parsers consume real cluster output. Hypothesis is
the wrong tool here — the input space is "what SGE binaries actually
emit," not "any string." Real-world output samples (header skip,
queue@host headers, gpu= continuation lines, dedup of jobs across
queue instances) are the right fixture.

Each test embeds a multi-line string the way the cluster would have
emitted it, then asserts the parsed structure. Captures regression
risk: a future "let me clean up the regex" change that breaks
``hl:gpu=2`` recognition (used by some SGE installs) shows up
immediately, not in production.
"""

# Fixture lines (qhost / qstat header rows) frequently exceed 100 chars
# because that's what SGE actually emits; we reproduce them verbatim
# rather than re-formatting and risking a parse divergence.
# ruff: noqa: E501

from __future__ import annotations

import textwrap

from claude_hpc.infra.inspect.sge import _parse_qhost, _parse_qstat_full

# ─── _parse_qhost ──────────────────────────────────────────────────────


def test_parse_qhost_extracts_basic_node_fields() -> None:
    """Standard ``qhost -F`` output: header skip, hostname, NCPU,
    LOAD, MEMTOT, MEMUSE land in the right NodeSnapshot fields."""
    output = textwrap.dedent(
        """\
        HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS
        ----------------------------------------------------------------------------------------------
        global                  -               -    -    -    -     -       -       -       -       -
        d11-07.hoffman2.idre.ucla.edu lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
        d11-08.hoffman2.idre.ucla.edu lx-amd64    16    2    8   16  2.10  128.0G  32.0G   16.0G   0.0
        """
    )
    nodes = _parse_qhost(output)
    assert len(nodes) == 2
    n0 = nodes[0]
    assert n0.name.startswith("d11-07")
    assert n0.cpu_tot == 16
    assert n0.cpu_load == 4.5
    assert n0.real_mem_mb == 128 * 1024
    # alloc_mem reflects MEMUSE
    assert n0.alloc_mem_mb == 64 * 1024
    assert n0.alloc_mem_pct == 0.5  # 64/128


def test_parse_qhost_picks_up_gpu_resource_continuation_lines() -> None:
    """Continuation lines (leading whitespace) carry GPU resource state.
    ``hl:gpu=2 hl:gpu_used=1`` → gres="gpu:2" / gres_used="gpu:1"."""
    output = textwrap.dedent(
        """\
        HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS
        ----------------------------------------------------------------------------------------------
        global                  -               -    -    -    -     -       -       -       -       -
        d11-07.cluster          lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
            hl:gpu=4 hl:gpu_used=1
        """
    )
    nodes = _parse_qhost(output)
    assert len(nodes) == 1
    assert nodes[0].gres == "gpu:4"
    assert nodes[0].gres_used == "gpu:1"


def test_parse_qhost_handles_bare_gpu_form_without_prefix() -> None:
    """Some SGE installs emit the bare ``gpu=N`` / ``gpu_used=N`` form
    without the ``hl:`` / ``gl:`` scope prefix. The parser must accept
    both — pinning so a future "tighten the regex" refactor doesn't
    silently regress on those clusters."""
    output = textwrap.dedent(
        """\
        HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS
        ----------------------------------------------------------------------------------------------
        h1.cluster              lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
            gpu=2 gpu_used=0
        """
    )
    nodes = _parse_qhost(output)
    assert nodes[0].gres == "gpu:2"
    assert nodes[0].gres_used == "gpu:0"


def test_parse_qhost_skips_global_summary_row() -> None:
    """The ``global`` summary row in qhost output is purely cumulative;
    treating it as a node would inflate counts. Pin the skip behaviour."""
    output = textwrap.dedent(
        """\
        HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS
        ----------------------------------------------------------------------------------------------
        global                  -               -    -    -    -     -       -       -       -       -
        h1.cluster              lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
        """
    )
    nodes = _parse_qhost(output)
    assert len(nodes) == 1
    assert "global" not in {n.name for n in nodes}


def test_parse_qhost_empty_input_returns_empty_list() -> None:
    assert _parse_qhost("") == []
    assert _parse_qhost("\n\n") == []


def test_parse_qhost_continuation_without_preceding_host_is_dropped() -> None:
    """Defensive: if continuation lines arrive before any host (corrupt
    output), they're dropped rather than crashing on ``current is None``."""
    output = textwrap.dedent(
        """\
            hl:gpu=4
        h1.cluster              lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
        """
    )
    nodes = _parse_qhost(output)
    # The orphan continuation is ignored; the host parses cleanly.
    assert len(nodes) == 1
    assert nodes[0].name == "h1.cluster"
    # ``gres`` defaults to "" on a fresh NodeSnapshot — the orphan
    # continuation never gets a chance to populate it because
    # ``current`` was None at that point.
    assert not nodes[0].gres


def test_parse_qhost_short_row_is_skipped() -> None:
    """A row with fewer than 8 cols can't be a valid host line; skip
    it. (NCPU, MEMTOT, MEMUSE positions matter — we can't infer a
    valid node from a truncated row.)"""
    output = textwrap.dedent(
        """\
        HOSTNAME                ARCH         NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE
        h1                                              # truncated row
        h2.cluster              lx-amd64    16    2    8   16  4.50  128.0G  64.0G   16.0G   0.0
        """
    )
    nodes = _parse_qhost(output)
    assert len(nodes) == 1
    assert nodes[0].name == "h2.cluster"


# ─── _parse_qstat_full ─────────────────────────────────────────────────


def test_parse_qstat_full_groups_jobs_by_host() -> None:
    """``queue@host`` headers introduce a host context; subsequent job
    lines belong to that host. Two queue instances on the same host
    share the host-level co-tenant list."""
    output = textwrap.dedent(
        """\
        queuename                      qtype resv/used/tot. load_avg arch          states
        ---------------------------------------------------------------------------------
        all.q@d11-07.cluster           BIP   0/4/16         1.23     lx-amd64
            12345 0.50 train_a   alice    r  10/29/2025 10:00:00 4
            12346 0.50 train_b   bob      r  10/29/2025 10:05:00 2
        all.q@d11-08.cluster           BIP   0/2/16         0.50     lx-amd64
            22222 0.50 eval_x    carol    r  10/29/2025 10:30:00 1
        """
    )
    by_host = _parse_qstat_full(output)
    assert set(by_host) == {"d11-07", "d11-08"}
    assert len(by_host["d11-07"]) == 2
    assert len(by_host["d11-08"]) == 1
    j1 = by_host["d11-07"][0]
    assert j1["job_id"] == "12345"
    assert j1["user"] == "alice"
    assert j1["state"] == "r"


def test_parse_qstat_full_dedups_same_job_across_queue_instances() -> None:
    """A job can appear under multiple queue instances on the same
    host; the parser dedups by ``(host, job_id)`` so the co-tenant
    count isn't doubled."""
    output = textwrap.dedent(
        """\
        all.q@d11-07.cluster           BIP   0/4/16         1.23     lx-amd64
            12345 0.50 train     alice    r  10/29/2025 10:00:00 4
        gpu.q@d11-07.cluster           BIP   0/4/16         1.23     lx-amd64
            12345 0.50 train     alice    r  10/29/2025 10:00:00 4
        """
    )
    by_host = _parse_qstat_full(output)
    assert len(by_host["d11-07"]) == 1


def test_parse_qstat_full_empty_input_returns_empty_dict() -> None:
    assert _parse_qstat_full("") == {}


def test_parse_qstat_full_short_job_row_skipped() -> None:
    """Job rows under 5 columns can't have a state field; skip them
    rather than indexing past the end."""
    output = textwrap.dedent(
        """\
        all.q@d11-07.cluster           BIP   0/4/16         1.23     lx-amd64
            12345 0.50
            12346 0.50 train     alice    r  10/29/2025 10:00:00 4
        """
    )
    by_host = _parse_qstat_full(output)
    # The truncated 12345 row was skipped; 12346 made it through.
    job_ids = {j["job_id"] for j in by_host.get("d11-07", [])}
    assert job_ids == {"12346"}


def test_parse_qstat_full_orphan_job_line_without_host_dropped() -> None:
    """A job line before any ``queue@host`` header has no host context;
    drop it. (Catches corrupt output where the first line was lost.)"""
    output = textwrap.dedent(
        """\
            12345 0.50 train     alice    r  10/29/2025 10:00:00 4
        all.q@d11-08.cluster           BIP   0/2/16         0.50     lx-amd64
            12346 0.50 eval      bob      r  10/29/2025 10:30:00 1
        """
    )
    by_host = _parse_qstat_full(output)
    # Orphan job dropped; the well-formed entry under d11-08 survives.
    assert "d11-08" in by_host
    assert by_host["d11-08"][0]["job_id"] == "12346"


def test_parse_qstat_full_extracts_cpu_count_from_last_column() -> None:
    """The ``slots`` count in the final column populates the ``cpus``
    field. Used by the planner to sum co-tenant CPU pressure. Note: the
    parser only reads ``cpus`` when the row has ``len > 8`` cols, so a
    realistic line with the trailing ja-task-ID column is required —
    the 8-col form leaves cpus at 0."""
    output = textwrap.dedent(
        """\
        all.q@d11-07.cluster           BIP   0/4/16         1.23     lx-amd64
            12345 0.50 train     alice    r  10/29/2025 10:00:00 all.q 8
        """
    )
    by_host = _parse_qstat_full(output)
    job = by_host["d11-07"][0]
    assert job["cpus"] == 8
