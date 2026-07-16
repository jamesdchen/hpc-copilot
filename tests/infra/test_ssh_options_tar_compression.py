"""tar-stream compression codec for the rsync-less tar|ssh legs (latency rank 7)."""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HPC_TAR_STREAM_COMPRESSION", raising=False)


def test_default_codec_is_gzip():
    # The VPN-friendly default: gzip is universal (GNU tar + bsdtar), no probe.
    assert ssh_options.tar_stream_codec() == "gzip"
    assert ssh_options.tar_stream_flag() == "-z"


@pytest.mark.parametrize(
    ("value", "codec", "flag"),
    [
        ("gzip", "gzip", "-z"),
        ("gz", "gzip", "-z"),
        ("GZIP", "gzip", "-z"),
        ("zstd", "zstd", "--zstd"),
        ("zst", "zstd", "--zstd"),
        ("none", "none", None),
        ("no", "none", None),
        ("off", "none", None),
        ("0", "none", None),
        ("plain", "none", None),
        ("default", "gzip", "-z"),
    ],
)
def test_env_selects_codec(monkeypatch, value, codec, flag):
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", value)
    assert ssh_options.tar_stream_codec() == codec
    assert ssh_options.tar_stream_flag() == flag


def test_fast_lan_opt_out_disables_compression(monkeypatch):
    # The census "fast-LAN opt-out": none -> a bare tar stream, no flag.
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", "none")
    assert ssh_options.tar_stream_flag() is None


def test_unrecognized_value_falls_to_gzip(monkeypatch, capsys):
    # Fail toward the VPN-safe default rather than silently ship uncompressed.
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", "lz4")
    assert ssh_options.tar_stream_codec() == "gzip"
    assert "unrecognized HPC_TAR_STREAM_COMPRESSION" in capsys.readouterr().err


def test_empty_value_is_default(monkeypatch):
    monkeypatch.setenv("HPC_TAR_STREAM_COMPRESSION", "")
    assert ssh_options.tar_stream_codec() == "gzip"
