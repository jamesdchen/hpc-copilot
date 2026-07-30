"""The readiness ledger's DURABLE tier (s2-readiness pillar 1).

Covers what the substrate promises: lockstep with the sensor layer's vocabulary
(one definition, two tiers), an atomic round-trip of ``VerdictAtom``-shaped
records, corrupt-file honesty (empty AND disclosed, never a crash), the overall
verdict ladder over ``{ready, stale, degraded, unknown}``, age math, and the
consult-then-durable read path.

Every timestamp is INJECTED — no test here reads a wall clock, so none can go
flaky at a horizon boundary or on a slow machine.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from hpc_agent.state import readiness
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

HOST = "login.example.edu"
T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> datetime:
    """``T0`` shifted by *seconds* — the only clock these tests have."""
    return T0 + timedelta(seconds=seconds)


def _atoms(host: str = HOST) -> list[dict[str, Any]]:
    atoms = readiness.read_ledger(host)["atoms"]
    assert isinstance(atoms, list)
    return atoms


# ---------------------------------------------------------------------------
# One vocabulary, two tiers
# ---------------------------------------------------------------------------


class TestVocabularyLockstep:
    def test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer(self) -> None:
        """The durable tier stores the SENSOR layer's unit of record, so its
        vocabulary must be that layer's — extended, never forked.

        Skips while ``infra/readiness_sensors.py`` is not present in the tree
        (this branch merges after it); once merged, the MIRROR comments in
        ``state/readiness.py`` are pinned here rather than merely asserted in
        prose.
        """
        sensors = pytest.importorskip("hpc_agent.infra.readiness_sensors")
        from dataclasses import fields
        from typing import get_args, get_type_hints

        assert get_args(sensors.SensorKind) == readiness.SENSOR_KINDS_FROM_SENSOR_LAYER
        assert get_args(sensors.SensorVerdict) == readiness.SENSOR_VERDICTS
        assert readiness.CONSULT_WINDOW_SEC == sensors.DEFAULT_FRESHNESS_WINDOW_SEC
        # VerdictAtom's fields are stored verbatim; ``source`` is the durable
        # tier's own additive metadata and is deliberately NOT one of them.
        assert tuple(f.name for f in fields(sensors.VerdictAtom)) == readiness.ATOM_FIELDS
        assert "source" not in readiness.ATOM_FIELDS
        # RESOLVE the annotations rather than reading ``Field.type``: the sensor
        # module has ``from __future__ import annotations``, so every ``.type`` is
        # the STRING "Literal['effective', ...]" and ``get_args`` on a string
        # returns () — which would have made this assertion vacuously pass on an
        # empty set and pinned nothing at all (2026-07-30 review, B2).
        route_type = get_type_hints(sensors.VerdictAtom)["route"]
        assert get_args(route_type), "route annotation did not resolve to a Literal"
        assert set(readiness.ROUTES) == set(get_args(route_type))

    def test_the_extension_collapsed_into_one_mirrored_vocabulary(self) -> None:
        """Pillar 3 built the four invariants' sensors, so the storage-side
        EXTENSION became a plain mirror: one definition, in the sensor layer.

        The four still appear LAST, in this order — ``cluster-readiness`` renders
        atoms by position in :data:`SENSOR_KINDS`, so the digest reads transport
        first and invariants after, which is the order a human diagnoses in.
        """
        assert readiness.SENSOR_KINDS == readiness.SENSOR_KINDS_FROM_SENSOR_LAYER
        assert readiness.SENSOR_KINDS[-4:] == (
            readiness.AUTH,
            readiness.SCRATCH,
            readiness.SCHEDULER,
            readiness.ENV,
        )
        # ...and the transport five still lead, unmoved.
        assert readiness.SENSOR_KINDS[:5] == (
            readiness.HOP,
            readiness.DIRECT,
            readiness.PATH,
            readiness.CONNECT,
            readiness.PREAMBLE,
        )

    def test_a_verdict_atom_dataclass_round_trips_through_the_store(self) -> None:
        """The write path accepts the sensor layer's own objects, duck-typed."""
        sensors = pytest.importorskip("hpc_agent.infra.readiness_sensors")
        atom = sensors.VerdictAtom(
            sensor="hop",
            target="usc-discovery",
            verdict="down",
            detail="refused",
            latency_ms=12.5,
            at=T0.isoformat(timespec="seconds"),
            at_epoch=T0.timestamp(),
            route="effective",
        )
        assert readiness.record_atoms(HOST, [atom], source="net-triage") == 1
        stored = _atoms()[0]
        assert stored["sensor"] == "hop"
        assert stored["target"] == "usc-discovery"
        assert stored["verdict"] == "down"
        assert stored["route"] == "effective"
        assert stored["source"] == "net-triage"
        # Reconstructable: every VerdictAtom field survives verbatim.
        assert (
            sensors.VerdictAtom(**{k: v for k, v in stored.items() if k in readiness.ATOM_FIELDS})
            == atom
        )


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_record_then_read_returns_the_atom(self) -> None:
        assert readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "ok",
            source="ssh-circuit",
            route="effective",
            latency_ms=41,
            detail="rc=0",
            now=T0,
        )
        atom = _atoms()[0]
        assert atom["sensor"] == readiness.CONNECT
        assert atom["verdict"] == "ok"
        assert atom["route"] == "effective"
        assert atom["target"] == HOST
        assert atom["latency_ms"] == 41
        assert atom["source"] == "ssh-circuit"
        assert atom["detail"] == "rc=0"
        assert readiness.atom_age_sec(atom, now=T0) == 0.0
        assert readiness.read_ledger(HOST)["corrupt"] is False

    def test_user_at_host_keys_identically_to_bare_host(self) -> None:
        """The breaker and the sensor layer both key on the bare host; the ledger
        MUST agree or an atom would land under a different subject."""
        readiness.record_observation(
            f"someone@{HOST}", readiness.CONNECT, "ok", source="ssh-circuit", now=T0
        )
        assert _atoms()[0]["verdict"] == "ok"

    def test_identity_is_sensor_route_target(self) -> None:
        """Two hops of one chain are distinct subjects, and the same sensor over
        the effective vs direct route is the dead-ProxyJump discriminator."""
        for target, route in (
            ("hop-a", "effective"),
            ("hop-b", "effective"),
            (HOST, "effective"),
            (HOST, "direct"),
        ):
            readiness.record_observation(
                HOST, readiness.HOP, "ok", source="s", target=target, route=route, now=T0
            )
        assert len(_atoms()) == 4

    def test_latest_observation_of_one_identity_wins(self) -> None:
        """A LEDGER, not a log."""
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="effective", now=T0
        )
        readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "down",
            source="s",
            route="effective",
            now=_at(readiness.MIN_REWRITE_SEC + 1),
        )
        assert [a["verdict"] for a in _atoms()] == ["down"]

    def test_unknown_sensor_and_verdict_are_ignored_not_written(self) -> None:
        """A typo must not create a phantom atom no reader interprets."""
        assert not readiness.record_observation(HOST, "connectt", "ok", source="s", now=T0)
        assert not readiness.record_observation(
            HOST, readiness.CONNECT, "failed", source="s", now=T0
        )
        assert _atoms() == []

    def test_an_unknown_route_degrades_to_n_a_rather_than_refusing(self) -> None:
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="sideways", now=T0
        )
        assert _atoms()[0]["route"] == "n/a"

    def test_empty_host_records_nothing(self) -> None:
        assert not readiness.record_observation("", readiness.CONNECT, "ok", source="s")
        assert readiness.record_atoms("", [{"sensor": "connect", "verdict": "ok"}]) == 0

    def test_the_stored_file_is_sorted_by_identity(self) -> None:
        """Stable bytes: a diff of two ledgers reflects verdicts, never write order."""
        readiness.record_observation(
            HOST, readiness.PREAMBLE, "ok", source="s", route="effective", now=T0
        )
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="effective", now=T0
        )
        raw = json.loads(readiness.readiness_path(HOST).read_text(encoding="utf-8"))
        identities = [readiness.atom_identity(a) for a in raw["atoms"]]
        assert identities == sorted(identities)


class TestRecordAtoms:
    def test_bulk_write_through_stores_every_atom(self) -> None:
        atoms = [
            {"sensor": "hop", "target": "jump", "verdict": "down", "route": "effective"},
            {"sensor": "direct", "target": HOST, "verdict": "ok", "route": "direct"},
            {"sensor": "path", "target": HOST, "verdict": "down", "route": "effective"},
        ]
        assert readiness.record_atoms(HOST, atoms, source="net-triage", now=T0) == 3
        assert {a["sensor"] for a in _atoms()} == {"hop", "direct", "path"}

    def test_an_undated_atom_is_stamped_so_it_is_never_permanently_stale(self) -> None:
        readiness.record_atoms(HOST, [{"sensor": "path", "verdict": "ok"}], now=T0)
        atom = _atoms()[0]
        assert readiness.atom_age_sec(atom, now=_at(30)) == 30.0

    def test_bulk_write_does_not_coalesce(self) -> None:
        """A composed sensor read is a deliberate, already-expensive act; its
        result must land whole, not be skipped as 'recently the same'."""
        readiness.record_atoms(
            HOST, [{"sensor": "path", "verdict": "ok", "route": "effective"}], now=T0
        )
        assert (
            readiness.record_atoms(
                HOST, [{"sensor": "path", "verdict": "ok", "route": "effective"}], now=_at(1)
            )
            == 1
        )
        assert readiness.atom_age_sec(_atoms()[0], now=_at(1)) == 0.0

    def test_foreign_sensors_in_a_bulk_write_are_dropped(self) -> None:
        assert (
            readiness.record_atoms(
                HOST,
                [{"sensor": "telepathy", "verdict": "ok"}, {"sensor": "path", "verdict": "ok"}],
                now=T0,
            )
            == 1
        )

    def test_a_bulk_write_upserts_beside_harvested_atoms(self) -> None:
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="ssh-circuit", route="effective", now=T0
        )
        readiness.record_atoms(HOST, [{"sensor": "hop", "target": "jump", "verdict": "ok"}], now=T0)
        assert {a["sensor"] for a in _atoms()} == {"connect", "hop"}


class TestCoalescing:
    def test_unchanged_atom_inside_the_window_skips_the_write(self) -> None:
        assert readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0
        )
        assert not readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "ok",
            source="ssh-circuit",
            now=_at(readiness.MIN_REWRITE_SEC - 1),
        )

    def test_a_changed_verdict_always_writes_however_recent(self) -> None:
        """Coalescing is for UNCHANGED atoms only — a flip must never be swallowed."""
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0)
        assert readiness.record_observation(
            HOST, readiness.CONNECT, "down", source="ssh-circuit", now=_at(1)
        )

    def test_a_different_source_always_writes(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0)
        assert readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="preflight", now=_at(1)
        )

    def test_a_different_identity_is_never_coalesced_against(self) -> None:
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="effective", now=T0
        )
        assert readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="direct", now=_at(1)
        )


# ---------------------------------------------------------------------------
# Corrupt-file honesty
# ---------------------------------------------------------------------------


class TestCorruptFileHonesty:
    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            "[]",
            '"a string"',
            '{"schema_version": 1, "host": "h", "atoms": {"connect": {}}}',
            "",
        ],
        ids=["torn", "list", "scalar", "atoms-not-a-list", "empty"],
    )
    def test_corrupt_reads_empty_and_disclosed_never_raises(self, raw: str) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        doc = readiness.read_ledger(HOST)
        assert doc["atoms"] == []
        assert doc["corrupt"] is True
        # And the verdict of an empty ledger is the honest one, never green.
        assert readiness.overall_verdict(doc, now=T0) == "unknown"

    def test_absent_file_is_empty_but_NOT_corrupt(self) -> None:
        """Nothing observed and unreadable are different facts."""
        doc = readiness.read_ledger("never-contacted.example")
        assert doc["atoms"] == []
        assert doc["corrupt"] is False

    def test_a_corrupt_file_is_rebuilt_by_the_next_observation(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn", encoding="utf-8")
        assert readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0
        )
        doc = readiness.read_ledger(HOST)
        assert doc["corrupt"] is False
        assert doc["atoms"][0]["verdict"] == "ok"

    def test_foreign_sensors_and_verdicts_are_dropped_on_read(self) -> None:
        """A foreign / future writer must never move this host's verdict."""
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "host": HOST,
                    "atoms": [
                        {"sensor": "telepathy", "verdict": "ok", "at": T0.isoformat()},
                        {"sensor": "connect", "verdict": "perfect", "at": T0.isoformat()},
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert readiness.read_ledger(HOST)["atoms"] == []

    def test_the_write_side_never_merges_into_an_unparseable_doc(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"atoms": [{"sensor": ', encoding="utf-8")
        readiness.record_observation(HOST, readiness.PREAMBLE, "ok", source="s", now=T0)
        assert [a["sensor"] for a in _atoms()] == [readiness.PREAMBLE]


# ---------------------------------------------------------------------------
# Age math (injected timestamps only)
# ---------------------------------------------------------------------------


class TestAgeMath:
    def test_age_is_the_difference_between_two_injected_instants(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        assert readiness.atom_age_sec(_atoms()[0], now=_at(125)) == 125.0

    def test_a_future_stamp_clamps_to_zero_not_negative(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=_at(60))
        assert readiness.atom_age_sec(_atoms()[0], now=T0) == 0.0

    def test_unparseable_stamp_has_no_age_and_is_stale(self) -> None:
        atom = {"sensor": "connect", "verdict": "ok", "at": "yesterday"}
        assert readiness.atom_age_sec(atom, now=T0) is None
        assert readiness.atom_is_stale(atom, now=T0) is True

    def test_horizon_is_per_sensor(self) -> None:
        assert readiness.stale_after_sec(readiness.CONNECT) == readiness.DEFAULT_STALE_AFTER_SEC
        assert readiness.stale_after_sec(readiness.ENV) > readiness.DEFAULT_STALE_AFTER_SEC

    def test_staleness_is_strictly_past_the_horizon(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        atom = _atoms()[0]
        horizon = readiness.stale_after_sec(readiness.CONNECT)
        assert not readiness.atom_is_stale(atom, now=_at(horizon))
        assert readiness.atom_is_stale(atom, now=_at(horizon + 1))


class TestLedgerAge:
    def test_freshest_atom_predating_the_instant_wins(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        readiness.record_observation(HOST, readiness.PREAMBLE, "ok", source="s", now=_at(100))
        assert readiness.ledger_age_sec(readiness.read_ledger(HOST), as_of=_at(300)) == 200.0

    def test_atoms_stamped_after_the_instant_are_excluded(self) -> None:
        """They did not exist yet — back-dating them would invent evidence."""
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        readiness.record_observation(HOST, readiness.PREAMBLE, "ok", source="s", now=_at(500))
        assert readiness.ledger_age_sec(readiness.read_ledger(HOST), as_of=_at(100)) == 100.0

    def test_no_atom_predating_the_instant_is_none_not_zero(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=_at(500))
        assert readiness.ledger_age_sec(readiness.read_ledger(HOST), as_of=T0) is None

    def test_empty_ledger_has_no_age(self) -> None:
        assert readiness.ledger_age_sec(readiness.read_ledger(HOST), as_of=T0) is None


# ---------------------------------------------------------------------------
# Consult-then-durable (the tier hook)
# ---------------------------------------------------------------------------


class TestConsultAtoms:
    def test_returns_only_atoms_inside_the_window(self) -> None:
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="effective", now=T0
        )
        readiness.record_observation(
            HOST, readiness.PREAMBLE, "ok", source="s", route="effective", now=_at(500)
        )
        fresh = readiness.consult_atoms(HOST, window_sec=120.0, now=_at(520))
        assert [a["sensor"] for a in fresh] == [readiness.PREAMBLE]

    def test_an_empty_ledger_consults_to_nothing_not_an_error(self) -> None:
        assert readiness.consult_atoms(HOST, now=T0) == []

    def test_the_default_window_is_the_sensor_layers_own(self) -> None:
        """Or a composer would re-dial legs the disk could have answered."""
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        assert readiness.consult_atoms(HOST, now=_at(readiness.CONSULT_WINDOW_SEC))
        assert not readiness.consult_atoms(HOST, now=_at(readiness.CONSULT_WINDOW_SEC + 1))


# ---------------------------------------------------------------------------
# The overall verdict ladder
# ---------------------------------------------------------------------------


def _doc(*atoms: tuple[str, str, float]) -> dict[str, Any]:
    """Build a ledger doc from ``(sensor, verdict, offset_seconds)`` triples."""
    return {
        "schema_version": 1,
        "host": HOST,
        "atoms": [
            {
                "sensor": sensor,
                "target": HOST,
                "verdict": verdict,
                "route": "effective",
                "at": _at(offset).isoformat(timespec="seconds"),
            }
            for sensor, verdict, offset in atoms
        ],
    }


class TestOverallVerdict:
    def test_no_atoms_is_unknown(self) -> None:
        assert readiness.overall_verdict(_doc(), now=T0) == "unknown"
        assert readiness.overall_verdict(None, now=T0) == "unknown"

    def test_all_required_fresh_and_ok_is_ready(self) -> None:
        assert readiness.overall_verdict(_doc(("connect", "ok", 0)), now=_at(10)) == "ready"

    def test_optional_sensors_do_not_block_ready_by_absence(self) -> None:
        """Only connect is required — the rest enrich."""
        assert readiness.overall_verdict(_doc(("connect", "ok", 0)), now=_at(10)) == "ready"

    @pytest.mark.parametrize("verdict", ["down", "timeout"])
    def test_a_fresh_failing_atom_is_degraded(self, verdict: str) -> None:
        doc = _doc(("connect", "ok", 0), ("preamble", verdict, 0))
        assert readiness.overall_verdict(doc, now=_at(10)) == "degraded"

    def test_a_stale_failure_is_stale_not_degraded(self) -> None:
        """The host may have healed and nothing has looked since — fencing a
        cluster on expired evidence is the mistake effective_state avoids."""
        doc = _doc(("connect", "down", 0))
        horizon = readiness.stale_after_sec(readiness.CONNECT)
        assert readiness.overall_verdict(doc, now=_at(horizon + 1)) == "stale"

    def test_missing_required_sensor_is_stale(self) -> None:
        assert readiness.overall_verdict(_doc(("preamble", "ok", 0)), now=_at(10)) == "stale"

    def test_a_stale_optional_atom_downgrades_ready_to_stale(self) -> None:
        doc = _doc(("connect", "ok", 0), ("path", "ok", 0))
        horizon = readiness.stale_after_sec(readiness.PATH)
        assert readiness.overall_verdict(doc, now=_at(horizon + 1)) == "stale"

    @pytest.mark.parametrize("verdict", ["unknown", "skipped"])
    def test_neither_unknown_nor_skipped_grants_ready(self, verdict: str) -> None:
        """'The sensor could not settle it' and 'it never ran' are not 'fine'."""
        doc = _doc(("connect", "ok", 0), ("scheduler", verdict, 0))
        assert readiness.overall_verdict(doc, now=_at(10)) == "stale"

    def test_every_verdict_is_in_the_declared_vocabulary(self) -> None:
        cases = [
            _doc(),
            _doc(("connect", "ok", 0)),
            _doc(("connect", "down", 0)),
            _doc(("connect", "timeout", 0)),
            _doc(("connect", "unknown", 0)),
            _doc(("connect", "skipped", 0)),
            _doc(("preamble", "ok", 0)),
        ]
        for doc in cases:
            for now in (T0, _at(10_000), _at(200_000)):
                assert readiness.overall_verdict(doc, now=now) in readiness.OVERALL_VERDICTS


class TestFutureStampsCannotReadForeverFresh:
    """F2 (2026-07-30 review): the render's clamp-to-zero must not feed staleness.

    Clamping a future stamp to age 0 is right for a renderer and catastrophic for
    a verdict — it made a year-9999 atom read ``ready`` forever, i.e. a document
    that certifies its own freshness in perpetuity.
    """

    def test_ordinary_skew_is_absorbed_and_still_reads_ready(self) -> None:
        skew = readiness.FUTURE_SKEW_TOLERANCE_SEC / 2
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=_at(skew))
        doc = readiness.read_ledger(HOST)
        assert readiness.overall_verdict(doc, now=T0) == "ready"

    def test_a_stamp_beyond_the_tolerance_is_stale_not_forever_ready(self) -> None:
        readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "ok",
            source="s",
            now=_at(readiness.FUTURE_SKEW_TOLERANCE_SEC + 60),
        )
        doc = readiness.read_ledger(HOST)
        assert readiness.overall_verdict(doc, now=T0) == "stale"

    def test_a_year_9999_atom_is_stale(self) -> None:
        """The reviewer's exhibit, pinned."""
        far = datetime(9999, 1, 1, tzinfo=timezone.utc)
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=far)
        doc = readiness.read_ledger(HOST)
        assert readiness.overall_verdict(doc, now=T0) == "stale"
        assert readiness.atom_is_stale(doc["atoms"][0], now=T0) is True

    def test_the_render_clamp_survives_for_rendering(self) -> None:
        """Staleness got stricter; the display age did NOT go negative."""
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=_at(60))
        assert readiness.atom_age_sec(_atoms()[0], now=T0) == 0.0
        assert readiness.atom_future_skew_sec(_atoms()[0], now=T0) == 60.0


class TestSchemaVersionIsValidatedOnRead:
    """F3: a version this build does not understand is not a doc to interpret."""

    def test_a_newer_schema_version_reads_corrupt_and_empty(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": readiness.SCHEMA_VERSION + 1,
                    "host": HOST,
                    "atoms": [
                        {
                            "sensor": "connect",
                            "verdict": "ok",
                            "at": T0.isoformat(timespec="seconds"),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        doc = readiness.read_ledger(HOST)
        assert doc["corrupt"] is True
        assert doc["atoms"] == []
        assert "newer than" in doc["corruption_reason"]
        assert readiness.overall_verdict(doc, now=T0) == "unknown"

    def test_a_missing_schema_version_reads_corrupt(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"host": HOST, "atoms": []}), encoding="utf-8")
        doc = readiness.read_ledger(HOST)
        assert doc["corrupt"] is True
        assert doc["corruption_reason"] == "no usable schema_version"

    def test_a_write_never_merges_into_a_future_versioned_doc(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": readiness.SCHEMA_VERSION + 1,
                    "host": HOST,
                    "atoms": [
                        {"sensor": "preamble", "verdict": "ok", "at": "2026-01-01T00:00:00Z"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        atoms = _atoms()
        assert [a["sensor"] for a in atoms] == [readiness.CONNECT]

    def test_every_read_reports_a_reason_or_none(self) -> None:
        assert readiness.read_ledger(HOST)["corruption_reason"] == ""


class TestCorruptionSurvivesTheRebuild:
    """F4: a rebuild that discards a corrupt file must not discard the FACT."""

    def test_the_reason_is_persisted_and_readable_after_the_rebuild(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn", encoding="utf-8")
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        doc = readiness.read_ledger(HOST)
        # The file is usable again ...
        assert doc["corrupt"] is False
        assert len(doc["atoms"]) == 1
        # ... but the operator can still learn that something was lost.
        last = doc["last_corruption"]
        assert last["reason"] == "not valid JSON"
        assert last["at"] == T0.isoformat(timespec="seconds")

    def test_the_note_is_carried_forward_by_later_clean_writes(self) -> None:
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn", encoding="utf-8")
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        readiness.record_observation(HOST, readiness.PREAMBLE, "ok", source="s", now=_at(600))
        assert readiness.read_ledger(HOST)["last_corruption"]["reason"] == "not valid JSON"

    def test_a_ledger_that_was_never_corrupt_carries_no_note(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        assert "last_corruption" not in readiness.read_ledger(HOST)


class TestFreshnessHasOneDefinitionPerTier:
    """``at`` is THE freshness ingredient here; ``at_epoch`` is carried, not read.

    The two can legitimately disagree — the sensor layer stamps ``at`` from the
    wall clock while ``at_epoch`` takes its injected ``now`` — so a reader that
    consulted whichever was handy would make an atom's age depend on who asked.
    """

    def test_age_follows_at_even_when_at_epoch_contradicts_it(self) -> None:
        readiness.record_atoms(
            HOST,
            [
                {
                    "sensor": "connect",
                    "target": HOST,
                    "verdict": "ok",
                    "route": "effective",
                    "at": T0.isoformat(timespec="seconds"),
                    # A wildly different epoch: if anything read this instead,
                    # the age below would not be 100.
                    "at_epoch": _at(-99999).timestamp(),
                }
            ],
            now=T0,
        )
        assert readiness.atom_age_sec(_atoms()[0], now=_at(100)) == 100.0

    def test_at_epoch_is_stored_verbatim_for_the_sensor_tier(self) -> None:
        """Carried, not rewritten — the sensor layer ages by it."""
        weird = _at(-99999).timestamp()
        readiness.record_atoms(
            HOST,
            [
                {
                    "sensor": "connect",
                    "verdict": "ok",
                    "at": T0.isoformat(timespec="seconds"),
                    "at_epoch": weird,
                }
            ],
            now=T0,
        )
        assert _atoms()[0]["at_epoch"] == weird

    def test_record_observation_stamps_both_from_one_instant(self) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        atom = _atoms()[0]
        assert atom["at"] == T0.isoformat(timespec="seconds")
        assert atom["at_epoch"] == T0.timestamp()


class TestTheLedgerStaysOutOfDeployTrees:
    """The checked assumption behind the 2026-07-30 rsync coupling (B1).

    This feed made the breaker's HEALTHY path a journal-home writer, so anything
    enumerating a tree that CONTAINS the journal home can see ledger files appear
    mid-walk. In production the two are disjoint by construction; the hazard is
    only reachable by a test that redirects the journal home into a directory it
    then pushes.
    """

    def test_the_ledger_lives_under_the_journal_home_not_an_experiment_dir(
        self, tmp_path: Any
    ) -> None:
        from hpc_agent.state.run_record import current_homedir

        experiment = tmp_path / "some-experiment"
        experiment.mkdir()
        ledger = readiness.readiness_path(HOST)
        assert current_homedir() in ledger.parents
        assert experiment not in ledger.parents

    def test_the_default_journal_home_is_not_inside_a_repo_tree(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """With no redirection the home is ``~/.claude/hpc`` — never a checkout,
        so a deploy push can never enumerate it."""
        import hpc_agent.state.run_record as rr

        monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
        monkeypatch.setattr(rr, "HPC_HOMEDIR", None, raising=False)
        home = rr.current_homedir()
        assert home.parts[-2:] == (".claude", "hpc")
        assert tmp_path not in home.parents

    def test_a_feed_writes_only_under_the_ledger_dir(self) -> None:
        """Pins the blast radius: one doc + its lock sentinel, nowhere else.

        The sentinel is deliberate repo-wide substrate (``infra.io.advisory_flock``
        re-touches it on release; ``test_lock_file_skipped_by_loader`` pins the
        behaviour), so it is asserted as EXPECTED here rather than treated as
        litter to remove.
        """
        from hpc_agent.state.run_record import current_homedir

        before = {p for p in current_homedir().rglob("*") if p.is_file()}
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        after = {p for p in current_homedir().rglob("*") if p.is_file()}
        new = after - before
        ledger_dir = readiness.readiness_path(HOST).parent
        assert new, "the feed wrote nothing"
        assert all(p.parent == ledger_dir for p in new), f"wrote outside the ledger dir: {new}"
        assert {p.name for p in new} <= {
            readiness.readiness_path(HOST).name,
            readiness.readiness_path(HOST).name + ".lock",
        }


class TestKnownHosts:
    def test_lists_every_host_with_a_ledger(self) -> None:
        readiness.record_observation("a.example", readiness.CONNECT, "ok", source="s", now=T0)
        readiness.record_observation("u@b.example", readiness.CONNECT, "down", source="s", now=T0)
        assert readiness.known_hosts() == ["a.example", "b.example"]

    def test_no_ledger_dir_is_empty_not_an_error(self) -> None:
        assert readiness.known_hosts() == []
