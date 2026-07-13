"""The conformance report line (K1 machinery unit test).

The verdict line renders exactly one of ``conforming: ...`` / ``partial: ...``,
names the kit version, and lists each skipped capability WITH its contract
degraded tier — the report never rounds partial up to conforming.
"""

from __future__ import annotations

from hpc_agent.conformance.adapter import (
    CAP_BACKGROUNDING,
    CAP_RELAY_ENFORCEMENT,
    CAP_UTTERANCE_LOG,
    CAPABILITIES,
    DEGRADED_TIERS,
)
from hpc_agent.conformance.report import (
    ConformanceReport,
    kit_version,
    render_report,
)

CAPABILITIES_ALL = CAPABILITIES


def test_conforming_line_renders() -> None:
    lines = render_report(
        adapter_name="claude-code",
        passed=CAPABILITIES_ALL,
        skipped=frozenset(),
        conforming=True,
        kit_version="1.2.3",
    )
    assert any(line == "conforming: harness contract v1 (kit hpc-agent 1.2.3)" for line in lines)
    assert any("claude-code" in line for line in lines)


def test_partial_line_lists_passed_and_skips_with_tier() -> None:
    lines = render_report(
        adapter_name="notebook-render",
        passed=frozenset({CAP_UTTERANCE_LOG}),
        skipped=frozenset({CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING}),
        conforming=False,
        kit_version="0.11.0",
    )
    headline = next(line for line in lines if line.startswith("partial:"))
    assert CAP_UTTERANCE_LOG in headline
    assert "kit hpc-agent 0.11.0" in headline
    # every skip names its contract tier verbatim
    for cap in (CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING):
        assert any(cap in line and DEGRADED_TIERS[cap] in line for line in lines)
    # partial is NEVER rendered as conforming
    assert not any(line.startswith("conforming:") for line in lines)


def test_report_dataclass_conforming() -> None:
    report = ConformanceReport(
        adapter_name="claude-code",
        declared=CAPABILITIES_ALL,
        skipped=frozenset(),
        failed=frozenset(),
        _kit_version="9.9.9",
    )
    assert report.conforming is True
    assert report.passed == CAPABILITIES_ALL
    assert any("conforming: harness contract v1" in line for line in report.to_lines())


def test_report_dataclass_partial_on_skip() -> None:
    report = ConformanceReport(
        adapter_name="notebook-render",
        declared=frozenset({CAP_UTTERANCE_LOG}),
        skipped=frozenset({CAP_RELAY_ENFORCEMENT, CAP_BACKGROUNDING}),
        _kit_version="9.9.9",
    )
    assert report.conforming is False
    assert report.passed == frozenset({CAP_UTTERANCE_LOG})


def test_report_dataclass_partial_on_failure() -> None:
    report = ConformanceReport(
        adapter_name="claude-code",
        declared=CAPABILITIES_ALL,
        skipped=frozenset(),
        failed=frozenset({CAP_RELAY_ENFORCEMENT}),
        _kit_version="9.9.9",
    )
    assert report.conforming is False
    assert CAP_RELAY_ENFORCEMENT not in report.passed


def test_kit_version_is_package_version() -> None:
    from hpc_agent import __version__

    assert kit_version() == __version__
