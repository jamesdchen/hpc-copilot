"""``Scheduler`` / ``BackendName`` validate against the live backend registry.

These wire aliases were a closed ``Literal`` over the four built-in SSH
families; #337 (Class A — enumeration) widened them to
``Annotated[str, AfterValidator(_validate_registered_backend)]`` so a registered
plugin backend (e.g. the pure-API github-actions backend) is expressible as a
spec everywhere a scheduler/backend name is accepted — without the orchestrator
hardcoding the scheduler list. The contract: every ``registered_backend_names``
member validates; an unregistered name is rejected with a clear error.
"""

from __future__ import annotations

import sys
import textwrap

import pytest
from pydantic import TypeAdapter, ValidationError

from hpc_agent._kernel.registry import plugins
from hpc_agent._wire._shared import BackendName, Scheduler
from hpc_agent.infra import backends

_BACKEND = TypeAdapter(BackendName)
_SCHEDULER = TypeAdapter(Scheduler)


@pytest.mark.parametrize("name", ["sge", "slurm", "pbspro", "torque"])
def test_builtin_backends_validate(name: str) -> None:
    assert _BACKEND.validate_python(name) == name
    assert _SCHEDULER.validate_python(name) == name


@pytest.mark.parametrize("bad", ["", "not-a-backend", "SLURM", "github actions"])
def test_unregistered_name_is_rejected(bad: str) -> None:
    # The error names the offending value AND the registered set so an
    # operator (or agent) can self-correct from the message alone.
    with pytest.raises(ValidationError, match="unknown backend"):
        _BACKEND.validate_python(bad)
    with pytest.raises(ValidationError, match="unknown backend"):
        _SCHEDULER.validate_python(bad)


def test_registered_plugin_backend_validates(tmp_path, monkeypatch) -> None:
    # A plugin backend registers at import time; once
    # ``registered_backend_names`` has imported it, the wire alias must accept
    # its name exactly like a built-in — that is the whole point of the widening
    # (a pure-API plugin backend expressible as a spec).
    mod = tmp_path / "fake_plugin_backend_for_wire.py"
    mod.write_text(
        textwrap.dedent(
            """\
            from hpc_agent.infra.backends import HPCBackend, register


            @register("fakewirebackend")
            class FakeWireBackend(HPCBackend):
                scheduler_name = "fakewirebackend"
                requires_ssh = False

                def _build_command(self, *a, **k):
                    raise NotImplementedError
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        plugins, "plugin_primitive_modules", lambda: ("fake_plugin_backend_for_wire",)
    )
    try:
        # Before the plugin is registered the name is unknown…
        assert "fakewirebackend" in backends.registered_backend_names()
        # …and now the wire alias accepts it (no SSH assumption).
        assert _BACKEND.validate_python("fakewirebackend") == "fakewirebackend"
        assert _SCHEDULER.validate_python("fakewirebackend") == "fakewirebackend"
    finally:
        backends._REGISTRY.pop("fakewirebackend", None)
        sys.modules.pop("fake_plugin_backend_for_wire", None)
