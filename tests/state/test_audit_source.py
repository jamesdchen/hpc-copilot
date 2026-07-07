"""Tests for the percent-format section model (``state/audit_source.py``) — the
notebook-audit source-of-truth parser (Wave A / T1).

Built from crafted percent-format strings: single/multi-section, preamble,
CRLF/LF hash equality, duplicate/invalid-slug + misplaced-marker refusals, the
template-and-source-parsed-identically property, and hash stability/sensitivity.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.state.audit_source import (
    normalize_source,
    parse_percent_source,
    sha256_normalized,
)

# ── crafted fixtures ────────────────────────────────────────────────────────

_SINGLE = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")
"""

_MULTI = """\
# %%
# hpc-audit-section: load-data
import pandas as pd
df = pd.read_csv("in.csv")

# %%
# a plain cell that belongs to load-data (no marker)
df = df.dropna()

# %%
# hpc-audit-section: fit-model
model = fit(df)
"""

_WITH_PREAMBLE = '''\
# %%
"""Module docstring — preamble, no section."""
import os

# %%
# hpc-audit-section: compute
x = os.cpu_count()
'''


def _slugs(text: str) -> list[str]:
    return list(parse_percent_source(text).slugs)


# ── segmentation ────────────────────────────────────────────────────────────


def test_single_section() -> None:
    mod = parse_percent_source(_SINGLE)
    assert _slugs(_SINGLE) == ["load-data"]
    assert mod.preamble == ""
    assert len(mod.sections) == 1
    assert mod.sections[0].section_sha == sha256_normalized(_SINGLE)


def test_multi_section_ordered() -> None:
    mod = parse_percent_source(_MULTI)
    assert _slugs(_MULTI) == ["load-data", "fit-model"]
    # The unmarked middle cell belongs to the FIRST section (marker cell plus
    # following cells until the next marker).
    assert "dropna" in mod.sections[0].source
    assert "dropna" not in mod.sections[1].source
    assert "fit(df)" in mod.sections[1].source
    # start_line ordering is monotonic.
    assert mod.sections[0].start_line < mod.sections[1].start_line


def test_preamble_belongs_to_no_section_but_is_in_module_sha() -> None:
    mod = parse_percent_source(_WITH_PREAMBLE)
    assert _slugs(_WITH_PREAMBLE) == ["compute"]
    assert "Module docstring" in mod.preamble
    # preamble text is in no section's source ...
    assert all("Module docstring" not in s.source for s in mod.sections)
    # ... but editing the preamble moves module_sha (whole-module hash).
    edited = _WITH_PREAMBLE.replace("preamble, no section", "preamble EDITED")
    assert parse_percent_source(edited).module_sha != mod.module_sha
    # while the compute section's hash is unchanged.
    assert parse_percent_source(edited).sections[0].section_sha == mod.sections[0].section_sha


def test_no_marker_is_all_preamble_zero_sections() -> None:
    text = "# %%\nimport os\nx = 1\n"
    mod = parse_percent_source(text)
    assert mod.sections == ()
    assert "import os" in mod.preamble
    assert mod.module_sha == sha256_normalized(text)


def test_markdown_cell_variant_is_an_opaque_boundary() -> None:
    text = (
        "# %%\n"
        "# hpc-audit-section: intro\n"
        "x = 1\n"
        "\n"
        "# %% [markdown]\n"
        "# Some prose in a markdown cell — still belongs to intro.\n"
        "\n"
        "# %%\n"
        "# hpc-audit-section: body\n"
        "y = 2\n"
    )
    mod = parse_percent_source(text)
    assert list(mod.slugs) == ["intro", "body"]
    assert "markdown" in mod.sections[0].source  # the [markdown] cell is in intro
    assert "y = 2" in mod.sections[1].source


# ── hashing: stability, sensitivity, cross-platform ─────────────────────────


def test_crlf_and_lf_hash_identically() -> None:
    lf = _MULTI
    crlf = _MULTI.replace("\n", "\r\n")
    cr = _MULTI.replace("\n", "\r")
    assert normalize_source(lf) == normalize_source(crlf) == normalize_source(cr)
    a, b, c = (parse_percent_source(t) for t in (lf, crlf, cr))
    assert a.module_sha == b.module_sha == c.module_sha
    assert [s.section_sha for s in a.sections] == [s.section_sha for s in b.sections]
    assert [s.section_sha for s in a.sections] == [s.section_sha for s in c.sections]


def test_trailing_whitespace_normalized_away() -> None:
    clean = _SINGLE
    trailing = _SINGLE.replace("import pandas as pd\n", "import pandas as pd   \n")
    assert sha256_normalized(clean) == sha256_normalized(trailing)


def test_hash_stability_same_input_same_sha() -> None:
    a = parse_percent_source(_MULTI)
    b = parse_percent_source(_MULTI)
    assert a.module_sha == b.module_sha
    assert [s.section_sha for s in a.sections] == [s.section_sha for s in b.sections]


def test_hash_sensitivity_one_section_edit_moves_only_that_section_and_module() -> None:
    before = parse_percent_source(_MULTI)
    # Edit ONLY the fit-model section's body, no line-count change.
    edited_text = _MULTI.replace("model = fit(df)", "model = fit(df, k=3)")
    after = parse_percent_source(edited_text)

    by_slug_before = {s.slug: s.section_sha for s in before.sections}
    by_slug_after = {s.slug: s.section_sha for s in after.sections}

    assert by_slug_after["fit-model"] != by_slug_before["fit-model"]  # moved
    assert by_slug_after["load-data"] == by_slug_before["load-data"]  # untouched
    assert after.module_sha != before.module_sha  # whole-module hash moved


# ── template parsed by the SAME function ────────────────────────────────────


def test_template_and_source_parse_identically() -> None:
    # A "template" and a "source" that share a section's exact content share
    # that section's section_sha — proving one parser serves both.
    template = (
        "# %%\n# hpc-audit-section: shared\nx = 1\n\n"
        "# %%\n# hpc-audit-section: drafted\npass  # TODO\n"
    )
    source = (
        "# %%\n# hpc-audit-section: shared\nx = 1\n\n"
        "# %%\n# hpc-audit-section: drafted\ny = compute()\n"
    )
    t = parse_percent_source(template)
    s = parse_percent_source(source)
    t_shared = next(sec for sec in t.sections if sec.slug == "shared")
    s_shared = next(sec for sec in s.sections if sec.slug == "shared")
    assert t_shared.section_sha == s_shared.section_sha  # inherited unchanged
    t_draft = next(sec for sec in t.sections if sec.slug == "drafted")
    s_draft = next(sec for sec in s.sections if sec.slug == "drafted")
    assert t_draft.section_sha != s_draft.section_sha  # drafted diverged


# ── loud refusals ───────────────────────────────────────────────────────────


def test_duplicate_slug_refused() -> None:
    text = "# %%\n# hpc-audit-section: dup\nx = 1\n\n# %%\n# hpc-audit-section: dup\ny = 2\n"
    with pytest.raises(errors.SpecInvalid, match="duplicate"):
        parse_percent_source(text)


@pytest.mark.parametrize("slug", ["has space", "bad/slash", "under#hash", ""])
def test_invalid_slug_refused(slug: str) -> None:
    text = f"# %%\n# hpc-audit-section: {slug}\nx = 1\n"
    with pytest.raises(errors.SpecInvalid):
        parse_percent_source(text)


def test_misplaced_marker_refused() -> None:
    # A col-0 marker that is NOT the cell's first non-blank line is misplaced:
    # loud, never a silent dropped section.
    text = "# %%\nimport os\n# hpc-audit-section: late\nx = 1\n"
    with pytest.raises(errors.SpecInvalid, match="first non-blank"):
        parse_percent_source(text)


def test_indented_marker_is_not_a_marker() -> None:
    # An INDENTED hpc-audit-section comment is ordinary in-body content, not a
    # marker — no section, no error (the recognition boundary is col 0).
    text = "# %%\ndef f():\n    # hpc-audit-section: inner\n    return 1\n"
    mod = parse_percent_source(text)
    assert mod.sections == ()
    assert "hpc-audit-section: inner" in mod.preamble


def test_marker_after_blank_lines_is_still_first_nonblank() -> None:
    # Blank lines before the marker do not disqualify it (first NON-BLANK).
    text = "# %%\n\n\n# hpc-audit-section: ok\nx = 1\n"
    mod = parse_percent_source(text)
    assert list(mod.slugs) == ["ok"]
