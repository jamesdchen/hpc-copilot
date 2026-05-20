"""Property-based tests for ``hpc_agent.infra.parsing``.

The module's design rules say it explicitly:

* **stdlib-only** — imported by cluster-side code that can't pull in
  third-party deps.
* **permissive** — every parser degrades to ``None`` / ``0`` rather
  than raising on garbage input. Schedulers vary across versions; we
  surface a partial answer instead of refusing to parse.

Both rules are property-shaped: "never raises" is total-function-ness;
"degrades to None / 0" is a typed-output contract. Examples can spot-
check a few inputs but the whole point of a permissive parser is that
the input space is hostile — exactly the kind of surface where
hypothesis finds bugs example tests never enumerate.

Coverage on this module before this file: 62%. After: should jump and
the remaining gaps are fall-throughs that example fixtures would cover.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hpc_agent.infra import parsing

# Hostile-input strategies: anything from an empty string to whatever
# weird tokens scheduler binaries emit. ``st.text()`` covers unicode,
# whitespace, control chars, and (with min_size=0) the empty string.
_anything = st.one_of(st.none(), st.text(max_size=64))


# ─── total-function-ness ───────────────────────────────────────────────


@given(_anything)
@settings(max_examples=75)
def test_to_int_never_raises_returns_int(s: str | None) -> None:
    """``to_int`` is the lossy-but-safe form. Pin: any input → int,
    no exception. Default returned for anything unparseable."""
    out = parsing.to_int(s, default=42)
    assert isinstance(out, int)


@given(_anything)
@settings(max_examples=75)
def test_to_int_or_none_never_raises_returns_int_or_none(s: str | None) -> None:
    out = parsing.to_int_or_none(s)
    assert out is None or isinstance(out, int)


@given(_anything)
@settings(max_examples=75)
def test_to_float_or_none_never_raises_returns_float_or_none(s: str | None) -> None:
    out = parsing.to_float_or_none(s)
    assert out is None or isinstance(out, float)


@given(_anything)
@settings(max_examples=75)
def test_parse_mem_to_gb_never_raises_returns_float_or_none(s: str | None) -> None:
    out = parsing.parse_mem_to_gb(s)
    assert out is None or isinstance(out, float)


@given(_anything)
@settings(max_examples=75)
def test_parse_mem_to_mb_never_raises_returns_int_or_none(s: str | None) -> None:
    out = parsing.parse_mem_to_mb(s)
    assert out is None or isinstance(out, int)


@given(_anything)
@settings(max_examples=75)
def test_parse_walltime_to_sec_never_raises_returns_nonneg_int(s: str | None) -> None:
    out = parsing.parse_walltime_to_sec(s)
    assert isinstance(out, int)
    assert out >= 0


@given(st.text(max_size=200))
@settings(max_examples=75)
def test_parse_qstat_columns_never_raises_returns_list_of_lists(text: str) -> None:
    """Tokeniser must survive any string the cluster might emit
    (header garbage, mid-line failures, control chars)."""
    out = parsing.parse_qstat_columns(text)
    assert isinstance(out, list)
    for row in out:
        assert isinstance(row, list)
        for col in row:
            assert isinstance(col, str)


# ─── round-trip / format invariants ─────────────────────────────────────


@given(st.integers(min_value=-(10**12), max_value=10**12))
@settings(max_examples=50)
def test_to_int_round_trips_pure_integer_strings(n: int) -> None:
    """For any int *n*, ``str(n)`` must parse back to *n*."""
    assert parsing.to_int(str(n), default=999) == n


@given(st.integers(min_value=0, max_value=10**9))
@settings(max_examples=50)
def test_to_int_accepts_trailing_dot_zero_form(n: int) -> None:
    """sacct emits ``CPUTimeRAW`` and similar as ``N.0``; the parser
    must accept that without falling back to default."""
    assert parsing.to_int(f"{n}.0", default=999) == n


@given(
    st.integers(min_value=0, max_value=99),
    st.integers(min_value=0, max_value=23),
    st.integers(min_value=0, max_value=59),
    st.integers(min_value=0, max_value=59),
)
@settings(max_examples=75)
def test_parse_walltime_round_trips_dhms_format(d: int, h: int, m: int, s: int) -> None:
    """``D-HH:MM:SS`` is the SLURM canonical walltime format. The
    parser must invert the formatter exactly."""
    text = f"{d}-{h:02d}:{m:02d}:{s:02d}"
    expected = d * 86400 + h * 3600 + m * 60 + s
    assert parsing.parse_walltime_to_sec(text) == expected


@given(
    st.integers(min_value=0, max_value=999),
    st.integers(min_value=0, max_value=59),
    st.integers(min_value=0, max_value=59),
)
@settings(max_examples=75)
def test_parse_walltime_round_trips_hms_format(h: int, m: int, s: int) -> None:
    """``HH:MM:SS`` (no day) — a common SLURM/SGE elapsed format."""
    text = f"{h}:{m:02d}:{s:02d}"
    expected = h * 3600 + m * 60 + s
    assert parsing.parse_walltime_to_sec(text) == expected


@given(st.integers(min_value=0, max_value=10**6))
@settings(max_examples=50)
def test_parse_walltime_accepts_bare_integer(n: int) -> None:
    """A bare integer is interpreted as raw seconds — used by some
    qstat formatters."""
    assert parsing.parse_walltime_to_sec(str(n)) == n


@given(st.integers(min_value=1, max_value=1024))
@settings(max_examples=50)
def test_parse_mem_to_gb_gigabyte_unit_round_trips(n: int) -> None:
    """For ``Ng`` / ``NG`` / ``NgB``, the value is *n* gigabytes."""
    for token in (f"{n}g", f"{n}G", f"{n}GB", f"{n}gb"):
        assert parsing.parse_mem_to_gb(token) == float(n), token


@given(st.integers(min_value=1, max_value=10000))
@settings(max_examples=50)
def test_parse_mem_to_mb_consistency_with_gb(n: int) -> None:
    """``parse_mem_to_mb`` must equal ``round(parse_mem_to_gb * 1024)``
    by construction; pinning so the conversion can't drift."""
    token = f"{n}G"
    gb = parsing.parse_mem_to_gb(token)
    mb = parsing.parse_mem_to_mb(token)
    assert gb is not None and mb is not None
    assert mb == int(round(gb * 1024))


# ─── parse_sacct_pipe_row contract ─────────────────────────────────────


_format_spec_strategy = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("z")),
        min_size=1,
        max_size=10,
    ).filter(lambda s: s.isidentifier()),
    min_size=1,
    max_size=8,
    unique=True,
)
_parts_strategy = st.lists(st.text(max_size=20), max_size=12)


@given(_parts_strategy, _format_spec_strategy)
@settings(max_examples=75)
def test_parse_sacct_row_returns_dict_keyed_by_format_spec(
    parts: list[str], format_spec: list[str]
) -> None:
    """Every key in *format_spec* must appear in the returned dict —
    callers rely on ``row[col]`` without a length check (the function's
    documented contract)."""
    row = parsing.parse_sacct_pipe_row(parts, format_spec)
    assert isinstance(row, dict)
    for col in format_spec:
        assert col in row, (col, row, parts, format_spec)


@given(_parts_strategy, _format_spec_strategy)
@settings(max_examples=75)
def test_parse_sacct_row_missing_trailing_cols_become_empty_string(
    parts: list[str], format_spec: list[str]
) -> None:
    """Trailing columns missing from *parts* fill with the empty string,
    not None / KeyError."""
    row = parsing.parse_sacct_pipe_row(parts, format_spec)
    for i, col in enumerate(format_spec):
        if i >= len(parts):
            assert row[col] == "", (col, row[col])


# ─── parse_qstat_columns prefix-skipping ────────────────────────────────


# Newline-like chars (``\r``, ``\n``, ``\v``, ``\f``, U+0085, U+2028,
# U+2029) all split lines under ``str.splitlines()``; filter them out
# so the constructed line stays one line.
_no_linebreaks = st.text(
    alphabet=st.characters(blacklist_categories=("Cc", "Zl", "Zp"), blacklist_characters="\n\r"),
    max_size=20,
)


@given(st.lists(_no_linebreaks, min_size=1, max_size=10))
@settings(max_examples=50)
def test_parse_qstat_columns_skips_documented_header_prefixes(words: list[str]) -> None:
    """A line whose first token starts with ``HOSTNAME`` / ``---`` /
    ``global`` / ``queuename`` / ``###`` must not appear in the output.
    Pinning this so a future "let me extend skip_prefixes" change can't
    silently start emitting headers."""
    for prefix in ("HOSTNAME", "---", "global", "queuename", "###"):
        line = prefix + " " + " ".join(words)
        rows = parsing.parse_qstat_columns(line)
        assert rows == [], (prefix, rows)
