"""Tests for the per-cluster validator helpers in
:mod:`hpc_agent.infra.clusters` for the PR-C survival-defense knobs.

Each helper applies a default and rejects wrong-typed yaml values so
e.g. ``walltime_arbitrage: "yes"`` (a string) doesn't silently flip the
feature on/off — the bad value fails loudly at load time.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import yaml

from hpc_agent import errors
from hpc_agent.infra.clusters import (
    _COLD_START_WALLTIME_SEC,
    _KNOWN_SCHEDULER_FAMILIES,
    ClusterConfig,
    get_auto_daisy_chain,
    get_default_walltime_sec,
    get_max_walltime_sec,
    get_walltime_arbitrage,
    remote_activation_for_sidecar,
    resolve_ssh_target,
)

# ─── known scheduler families (pbspro / torque wiring) ───────────────────────


class TestKnownSchedulerFamilies:
    def test_pbs_families_are_known(self):
        # The frozen family-name strings the engine registers under.
        assert frozenset({"slurm", "sge", "pbspro", "torque"}) == _KNOWN_SCHEDULER_FAMILIES

    @pytest.mark.parametrize("fam", ["slurm", "sge", "pbspro", "torque"])
    def test_known_family_needs_no_pin(self, fam):
        # A cluster can declare a known family with no scheduler_profile pin.
        cfg = ClusterConfig.model_validate({"scheduler": fam, "host": "h", "user": "u"})
        assert cfg.scheduler == fam
        assert cfg.scheduler_profile is None

    def test_unknown_family_still_requires_pin(self):
        with pytest.raises(errors.SpecInvalid, match="not a known family"):
            ClusterConfig.model_validate({"scheduler": "moab"})

    def test_plugin_registered_backend_needs_no_pin(self):
        # The crowd-compute seam: a backend name registered by a plugin
        # (here: registered directly, the same @register call a plugin's
        # primitive_modules import runs) validates without a pinned
        # scheduler_profile — see docs/proposals/crowd-compute-backend.md.
        from hpc_agent.infra.backends import _REGISTRY, HPCBackend, register

        @register("fakecrowd")
        class _FakeCrowdBackend(HPCBackend):
            scheduler_name = "fakecrowd"

            def _build_command(self, *a, **k):  # pragma: no cover - never called
                raise NotImplementedError

        try:
            cfg = ClusterConfig.model_validate({"scheduler": "fakecrowd"})
            assert cfg.scheduler == "fakecrowd"
            assert cfg.scheduler_profile is None
        finally:
            _REGISTRY.pop("fakecrowd", None)
        # And once unregistered, the same entry is rejected again — the
        # acceptance really came from the registry, not from a cache.
        with pytest.raises(errors.SpecInvalid, match="not a known family"):
            ClusterConfig.model_validate({"scheduler": "fakecrowd"})


# ─── get_walltime_arbitrage ─────────────────────────────────────────────────


class TestGetWalltimeArbitrage:
    def test_default_true(self):
        # Absent key -> default True (the helper is opt-out, not opt-in).
        assert get_walltime_arbitrage({}) is True

    def test_explicit_true(self):
        assert get_walltime_arbitrage({"walltime_arbitrage": True}) is True

    def test_explicit_false(self):
        assert get_walltime_arbitrage({"walltime_arbitrage": False}) is False

    @pytest.mark.parametrize(
        "bad",
        ["yes", "true", 1, 0, 1.0, [], {}, None],
    )
    def test_rejects_non_bool(self, bad):
        with pytest.raises(errors.SpecInvalid, match="walltime_arbitrage"):
            get_walltime_arbitrage({"walltime_arbitrage": bad})


# ─── get_auto_daisy_chain ───────────────────────────────────────────────────


class TestGetAutoDaisyChain:
    def test_absent_returns_none(self):
        # Absent key -> None ("use detection").
        assert get_auto_daisy_chain({}) is None

    def test_explicit_none_returns_none(self):
        # Explicit None -> None (same as absent).
        assert get_auto_daisy_chain({"auto_daisy_chain": None}) is None

    def test_explicit_true(self):
        # Always-chain override.
        assert get_auto_daisy_chain({"auto_daisy_chain": True}) is True

    def test_explicit_false(self):
        # Kill switch — never chain on this cluster.
        assert get_auto_daisy_chain({"auto_daisy_chain": False}) is False

    @pytest.mark.parametrize("bad", ["yes", "true", 1, 0, 1.0, []])
    def test_rejects_non_bool(self, bad):
        with pytest.raises(errors.SpecInvalid, match="auto_daisy_chain"):
            get_auto_daisy_chain({"auto_daisy_chain": bad})


# ─── get_max_walltime_sec ───────────────────────────────────────────────────


class TestGetMaxWalltimeSec:
    def test_default_24h(self):
        # Absent key -> 86400s (24h), a typical campus-cluster ceiling.
        assert get_max_walltime_sec({}) == 86400

    def test_explicit_value(self):
        assert get_max_walltime_sec({"max_walltime_sec": 172800}) == 172800

    def test_rejects_zero(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": 0})

    def test_rejects_negative(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_max_walltime_sec({"max_walltime_sec": -1})

    @pytest.mark.parametrize("bad", ["86400", 86400.0, True, False, [86400], None])
    def test_rejects_non_int(self, bad):
        with pytest.raises(errors.SpecInvalid, match="max_walltime_sec"):
            get_max_walltime_sec({"max_walltime_sec": bad})


# ─── get_default_walltime_sec (cold-start fallback, #170) ────────────────────


class TestGetDefaultWalltimeSec:
    def test_absent_returns_conservative_floor(self):
        # No prior, no operator override, no optional prior-reading verb: the
        # fallback MUST still resolve (#170) to the conservative built-in floor.
        assert get_default_walltime_sec({}) == _COLD_START_WALLTIME_SEC

    def test_explicit_value_used(self):
        assert get_default_walltime_sec({"default_walltime_sec": 7200}) == 7200

    def test_floor_clamped_to_max_walltime(self):
        # A small-ceiling cluster never gets a cold-start ask above what its
        # scheduler accepts — the floor is clamped to max_walltime_sec.
        cfg = {"max_walltime_sec": 3600}
        assert get_default_walltime_sec(cfg) == 3600

    def test_explicit_value_clamped_to_max_walltime(self):
        cfg = {"default_walltime_sec": 999999, "max_walltime_sec": 7200}
        assert get_default_walltime_sec(cfg) == 7200

    def test_rejects_zero(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_default_walltime_sec({"default_walltime_sec": 0})

    def test_rejects_negative(self):
        with pytest.raises(errors.SpecInvalid, match="positive"):
            get_default_walltime_sec({"default_walltime_sec": -1})

    @pytest.mark.parametrize("bad", ["7200", 7200.0, True, False, [7200]])
    def test_rejects_non_int(self, bad):
        # None is NOT in this list: an absent key is the valid cold-start path.
        with pytest.raises(errors.SpecInvalid, match="default_walltime_sec"):
            get_default_walltime_sec({"default_walltime_sec": bad})

    def test_demo_clusters_yaml_resolves_for_every_cluster(self):
        # The shipped clusters.yaml has no default_walltime_sec (issue #170's
        # second gap); the resolver must still produce a value for each stanza.
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
        assert clusters  # sanity: the packaged config loaded
        for name, cfg in clusters.items():
            wt = get_default_walltime_sec(cfg)
            assert wt > 0, name
            assert wt <= get_max_walltime_sec(cfg), name


# ─── resolve_ssh_target — use-time host resolution (run12 finding 23 / RULING 1) ──


class TestResolveSshTarget:
    """``ssh_target`` is CONFIG resolved fresh from clusters.yaml at USE time; the
    journal records only the CLUSTER key (history). A login-node failover is a
    config edit, never journal surgery — and when config can't answer, the frozen
    submit-time target is the disclosed migration-shim fallback.
    """

    def _point_config_at(self, tmp_path, monkeypatch, mapping):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.safe_dump(mapping), encoding="utf-8")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(p))
        return p

    def test_host_change_resolves_to_new_host_with_no_record_surgery(self, tmp_path, monkeypatch):
        # The record was submitted when `discovery` pointed at discovery2; its
        # FROZEN ssh_target still says discovery2. A login-node failover edits
        # clusters.yaml to discovery1 — a CONFIG change, no journal surgery.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {"discovery": {"scheduler": "slurm", "user": "jc", "host": "discovery1.usc.edu"}},
        )
        record = SimpleNamespace(cluster="discovery", ssh_target="jc@discovery2.usc.edu")

        # Use-time resolution yields the NEW host from config...
        assert resolve_ssh_target(record) == "jc@discovery1.usc.edu"
        # ...and the record was never rewritten (frozen provenance intact).
        assert record.ssh_target == "jc@discovery2.usc.edu"

    def test_matching_config_returns_live_target(self, tmp_path, monkeypatch):
        # When config still agrees with the frozen value, the live target is used
        # (and equals the frozen one) — no fallback path taken.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {"hoffman2": {"scheduler": "sge", "user": "u", "host": "hoffman2.idre.ucla.edu"}},
        )
        record = SimpleNamespace(cluster="hoffman2", ssh_target="u@hoffman2.idre.ucla.edu")
        assert resolve_ssh_target(record) == "u@hoffman2.idre.ucla.edu"

    def test_template_placeholder_falls_back_to_frozen_and_discloses(
        self, tmp_path, monkeypatch, caplog
    ):
        # The PACKAGED clusters.yaml template carries '<your_user>@...'
        # placeholders. A derivation the transport would refuse is NOT a live
        # resolution (the CI environment has only the template — every
        # aggregate/monitor test crashed SpecInvalid on '<'/'>' before this
        # guard): fall back to the frozen submit-time target, disclosed.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {
                "hoffman2": {
                    "scheduler": "sge",
                    "user": "<your_user>",
                    "host": "hoffman2.idre.ucla.edu",
                }
            },
        )
        record = SimpleNamespace(cluster="hoffman2", ssh_target="u@h")

        with caplog.at_level(logging.WARNING, logger="hpc_agent.infra.clusters"):
            resolved = resolve_ssh_target(record)

        assert resolved == "u@h"  # frozen value used, placeholder never escapes
        assert "not a usable ssh target" in caplog.text

    def test_missing_cluster_key_falls_back_to_frozen_and_discloses(
        self, tmp_path, monkeypatch, caplog
    ):
        # clusters.yaml is populated but does NOT define the record's cluster (an
        # ad-hoc cluster, or one removed after submit) → the FROZEN submit-time
        # target is used and the fallback is DISCLOSED on the log.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {"hoffman2": {"scheduler": "sge", "user": "u", "host": "hoffman2.idre.ucla.edu"}},
        )
        record = SimpleNamespace(cluster="adhoc-box", ssh_target="me@adhoc.example.edu")

        with caplog.at_level(logging.WARNING, logger="hpc_agent.infra.clusters"):
            resolved = resolve_ssh_target(record)

        assert resolved == "me@adhoc.example.edu"  # frozen value used
        assert "adhoc-box" in caplog.text
        assert "absent from clusters.yaml" in caplog.text

    def test_record_predating_cluster_field_falls_back_and_discloses(self, caplog):
        # A record minted before the cluster field existed carries no cluster key
        # (empty) → nothing to resolve from config; the frozen target is used and
        # the fallback is disclosed.
        record = SimpleNamespace(cluster="", ssh_target="legacy@old.host.edu")
        with caplog.at_level(logging.WARNING, logger="hpc_agent.infra.clusters"):
            resolved = resolve_ssh_target(record)
        assert resolved == "legacy@old.host.edu"
        assert "no cluster key" in caplog.text

    def test_cluster_without_derivable_target_falls_back_and_discloses(
        self, tmp_path, monkeypatch, caplog
    ):
        # The cluster entry exists but yields no user@host (no `user`) — the frozen
        # value is used and the fallback is disclosed.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {"adhoc": {"scheduler": "sge", "host": "adhoc.example.edu"}},  # no user
        )
        record = SimpleNamespace(cluster="adhoc", ssh_target="frozen@adhoc.example.edu")
        with caplog.at_level(logging.WARNING, logger="hpc_agent.infra.clusters"):
            resolved = resolve_ssh_target(record)
        assert resolved == "frozen@adhoc.example.edu"
        assert "no derivable user@host" in caplog.text


# ─── FIX A: preamble-free control plane (run-13 finding 10 / run-14) ──────────


class TestControlPlaneDirectInterpreter:
    """``remote_activation_for_sidecar`` composes the control-plane command with
    NO module/conda ceremony when a DIRECT env interpreter is derivable, and
    keeps the legacy preamble (backwards compatible, disclosed) when it is not.
    """

    def test_env_python_yields_preamble_free_prefix(self):
        # An absolute env_python → the control-plane prefix is a bare PATH-prepend
        # to the interpreter's bin dir; NO `module load`, `source .../conda.sh` or
        # `conda activate` (the stages that hang on a degraded /apps mount).
        import hpc_agent.infra.clusters as clusters_mod

        direct = clusters_mod._control_plane_direct_prefix(
            "/u/home/me/.conda/envs/harxhar/bin/python"
        )
        assert direct == 'export PATH=/u/home/me/.conda/envs/harxhar/bin:"$PATH" && '
        assert "module load" not in direct
        assert "conda.sh" not in direct
        assert "conda activate" not in direct

    def test_sidecar_env_python_composes_preamble_free_via_deriver(self):
        # End-to-end through remote_activation_for_sidecar: an env_python pinned in
        # the sidecar env wins and yields the preamble-free prefix.
        prefix = remote_activation_for_sidecar(
            {"env": {"env_python": "~/.conda/envs/harxhar/bin/python"}}
        )
        assert prefix == 'export PATH=~/.conda/envs/harxhar/bin:"$PATH" && '
        # The `~` is emitted UNQUOTED so the REMOTE shell expands it (MSYS trap).
        assert '"~' not in prefix

    def test_no_env_python_keeps_legacy_preamble(self, tmp_path, monkeypatch):
        # A cluster WITHOUT env_python keeps the module/conda preamble unchanged —
        # backwards compatible (the breaker's preamble-degradation classifier still
        # sees the module/conda markers for these clusters).
        p = tmp_path / "clusters.yaml"
        p.write_text(
            yaml.safe_dump(
                {
                    "carc": {
                        "scheduler": "slurm",
                        "user": "me",
                        "host": "discovery2.usc.edu",
                        "conda_source": "/apps/anaconda3/etc/profile.d/conda.sh",
                        "conda_envs": ["harxhar"],
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(p))
        prefix = remote_activation_for_sidecar({"cluster": "carc"}, fallback_cluster="carc")
        assert "source" in prefix and "conda.sh" in prefix
        assert "conda activate" in prefix

    def test_shell_unsafe_env_python_falls_back_to_preamble(self):
        import hpc_agent.infra.clusters as clusters_mod

        # A space (or any shell metachar) in the path → unsafe to emit unquoted →
        # None → the caller keeps the legacy preamble.
        assert clusters_mod._control_plane_direct_prefix("/opt/my env/bin/python") is None
        # A bare interpreter with no directory component is also unusable.
        assert clusters_mod._control_plane_direct_prefix("python") is None


# ─── FIX B: login-pool aware use-time resolution ─────────────────────────────


class TestResolveSshTargetPool:
    """``resolve_ssh_target`` honors a per-run pool-member choice (the frozen
    ssh_target's host, when it is a member of the cluster's login_pool) so an
    automatic failover sticks across polls; single-host entries are unchanged."""

    def _point_config_at(self, tmp_path, monkeypatch, mapping):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.safe_dump(mapping), encoding="utf-8")
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(p))
        return p

    def test_pool_member_frozen_host_is_honored_over_default(self, tmp_path, monkeypatch):
        # carc's default host is discovery2, but login_pool lists discovery1. A run
        # that failed over has frozen ssh_target = me@discovery1 → resolve honors
        # that member instead of snapping back to the config default discovery2.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {
                "carc": {
                    "scheduler": "slurm",
                    "user": "me",
                    "host": "discovery2.usc.edu",
                    "login_pool": ["discovery1.usc.edu"],
                }
            },
        )
        record = SimpleNamespace(cluster="carc", ssh_target="me@discovery1.usc.edu")
        assert resolve_ssh_target(record) == "me@discovery1.usc.edu"

    def test_pool_default_host_still_resolves_when_not_failed_over(self, tmp_path, monkeypatch):
        # No failover yet: frozen host == default host → same value; user stays
        # config-driven.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {
                "carc": {
                    "scheduler": "slurm",
                    "user": "me",
                    "host": "discovery2.usc.edu",
                    "login_pool": ["discovery1.usc.edu"],
                }
            },
        )
        record = SimpleNamespace(cluster="carc", ssh_target="me@discovery2.usc.edu")
        assert resolve_ssh_target(record) == "me@discovery2.usc.edu"

    def test_frozen_host_not_in_pool_falls_through_to_config(self, tmp_path, monkeypatch):
        # The operator removed the old member from the pool → the frozen host is no
        # longer a member → config-wins derivation returns the default host (RULING
        # 1 control handed back to config).
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {
                "carc": {
                    "scheduler": "slurm",
                    "user": "me",
                    "host": "discovery2.usc.edu",
                    "login_pool": ["discovery3.usc.edu"],
                }
            },
        )
        record = SimpleNamespace(cluster="carc", ssh_target="me@discovery1.usc.edu")
        assert resolve_ssh_target(record) == "me@discovery2.usc.edu"

    def test_single_host_entry_is_unchanged(self, tmp_path, monkeypatch):
        # No login_pool → the pool branch is a strict no-op; config-wins as before.
        self._point_config_at(
            tmp_path,
            monkeypatch,
            {"carc": {"scheduler": "slurm", "user": "me", "host": "discovery2.usc.edu"}},
        )
        record = SimpleNamespace(cluster="carc", ssh_target="me@discovery1.usc.edu")
        # Config wins (the RULING-1 behavior) — resolves to the config host.
        assert resolve_ssh_target(record) == "me@discovery2.usc.edu"
