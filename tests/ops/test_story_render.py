"""Tests for the run-story render (``ops/story_render.py``, T2).

Canonical JSON + ``story_sha`` + code-rendered markdown. Covers golden markdown,
sha stability across dict insertion orders + sensitivity to a real change, window
header honesty (an omitted_count > 0 render WITHOUT the "omitted" line FAILS), and
the counts-only rule (a metric VALUE crafted into an event's evidence never
reaches the rendered line).
"""

from __future__ import annotations

from hpc_agent.ops import story_render as sr
from hpc_agent.state.run_story import StoryEvent

_H = {"run_ids": ["r1"], "cluster": "hoffman2", "status": "complete"}


def _events() -> list[StoryEvent]:
    return [
        StoryEvent(
            ts="2026-07-08T12:00:00+00:00",
            stream="briefs",
            actor="code",
            kind="s1",
            subject_id="r1",
            evidence={"brief_digest": "abc123"},
        ),
        StoryEvent(
            ts="2026-07-08T12:00:00+00:00",
            stream="decision-journal",
            actor="human",
            kind="s1",
            subject_id="r1",
            text="ship it",
        ),
        StoryEvent(
            ts="2026-07-08T13:00:00+00:00",
            stream="look-ledger",
            actor="code",
            kind="look",
            subject_id="r1",
            evidence={"scope": "holdout", "cmd_sha": "csha"},
        ),
    ]


# ── canonical JSON + story_sha ────────────────────────────────────────────────


def test_event_payload_is_exactly_the_d3_key_set() -> None:
    payload = sr.event_payload(_events()[0])
    assert tuple(sorted(payload)) == tuple(sorted(sr.EVENT_KEYS))


def test_story_sha_stable_across_dict_insertion_order() -> None:
    header_a = {"cluster": "hoffman2", "run_ids": ["r1"], "status": "complete"}
    header_b = {"status": "complete", "run_ids": ["r1"], "cluster": "hoffman2"}
    p_a = sr.story_payload(header_a, _events(), total_events=3, omitted_count=0)
    p_b = sr.story_payload(header_b, _events(), total_events=3, omitted_count=0)
    assert sr.story_sha(p_a) == sr.story_sha(p_b)


def test_story_sha_sensitive_to_content_and_counts() -> None:
    base = sr.story_payload(_H, _events(), total_events=3, omitted_count=0)
    # A changed event moves the sha.
    changed_events = _events()
    changed_events[1] = StoryEvent(
        ts="2026-07-08T12:00:00+00:00",
        stream="decision-journal",
        actor="human",
        kind="s1",
        subject_id="r1",
        text="do NOT ship it",
    )
    changed = sr.story_payload(_H, changed_events, total_events=3, omitted_count=0)
    assert sr.story_sha(base) != sr.story_sha(changed)
    # The omission counts are load-bearing in the pre-image (D6): same events,
    # different window → different sha.
    windowed = sr.story_payload(_H, _events()[1:], total_events=3, omitted_count=1)
    assert sr.story_sha(base) != sr.story_sha(windowed)


# ── markdown ──────────────────────────────────────────────────────────────────


def test_golden_markdown() -> None:
    render = sr.render_story(_H, _events(), total_events=3, omitted_count=0)
    assert render.markdown == (
        "# Run story\n"
        "\n"
        "- 3 event(s)\n"
        "- cluster: hoffman2\n"
        "- run_ids: r1\n"
        "- status: complete\n"
        "\n"
        "## s1\n"
        "\n"
        "- 2026-07-08T12:00:00+00:00 · code · s1 · r1 · brief_digest=abc123\n"
        '- 2026-07-08T12:00:00+00:00 · human · s1 · r1 · "ship it"\n'
        "\n"
        "## look\n"
        "\n"
        "- 2026-07-08T13:00:00+00:00 · code · look · r1 · cmd_sha=csha, scope=holdout\n"
    )


def test_window_header_honesty() -> None:
    render = sr.render_story(_H, _events()[1:], total_events=3, omitted_count=1)
    assert "showing 2 of 3 events (1 older events omitted)" in render.markdown
    # An omitted render must NEVER hide the omission — the "omitted" line is present.
    assert "omitted" in render.markdown


def test_no_window_renders_plain_count() -> None:
    render = sr.render_story(_H, _events(), total_events=3, omitted_count=0)
    assert "- 3 event(s)" in render.markdown
    assert "omitted" not in render.markdown


def test_counts_only_metric_value_never_renders() -> None:
    # A crafted event carrying a metric VALUE in evidence: the value must be
    # DROPPED from the rendered line (counts-only rule, D3).
    crafted = StoryEvent(
        ts="2026-07-08T12:00:00+00:00",
        stream="briefs",
        actor="code",
        kind="s4",
        subject_id="r1",
        evidence={"accuracy": 0.9731, "row_count": 20, "cmd_sha": "csha"},
    )
    render = sr.render_story(_H, [crafted], total_events=1, omitted_count=0)
    assert "0.9731" not in render.markdown  # the metric value never reaches the line
    assert "accuracy" not in render.markdown
    assert "row_count=20" in render.markdown  # a COUNT renders
    assert "cmd_sha=csha" in render.markdown  # a pointer renders


def test_c2_finding_cause_class_disposition_render() -> None:
    # A Class-C2 overnight finding renders its cause, class, and report-only
    # disposition (identity/classification literals on the whitelist) — the story is
    # where science lands (overnight-repair §4.4/§7.4).
    finding = StoryEvent(
        ts="2026-07-08T17:00:00+00:00",
        stream="overnight-ledger",
        actor="code",
        kind="c2-finding",
        subject_id="r1",
        evidence={"cause": "result-anomaly", "heal_class": "C2", "disposition": "report-only"},
    )
    render = sr.render_story(_H, [finding], total_events=1, omitted_count=0)
    assert "c2-finding" in render.markdown
    assert "cause=result-anomaly" in render.markdown
    assert "heal_class=C2" in render.markdown
    assert "disposition=report-only" in render.markdown


def test_empty_story_renders_without_error() -> None:
    render = sr.render_story(_H, [], total_events=0, omitted_count=0)
    assert "(no events)" in render.markdown
    assert render.story_sha  # a fingerprint over the empty (but header-bearing) payload
    assert render.payload["events"] == []


def test_markdown_opt_out() -> None:
    render = sr.render_story(_H, _events(), total_events=3, omitted_count=0, markdown=False)
    assert render.markdown == ""
    assert render.story_sha  # sha still computed
