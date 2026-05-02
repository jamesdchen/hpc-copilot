"""Reference fixture: shape of a user-authored ``.hpc/stages.py``.

Used by ``tests/test_stages_loader.py`` and serves as the canonical
example replacing the legacy ``hpc_multistage.yaml`` fixture.
"""

from __future__ import annotations


def stages() -> list[dict]:
    return [
        {
            "name": "prepare",
            "run": "python -m myexp.prepare",
            "resources": {"cpus": 1, "mem": "8G", "walltime": "0:30:00"},
        },
        {
            "name": "fit",
            "run": "python -m myexp.fit",
            "depends_on": "prepare",
            "resources": {
                "cpus": 8,
                "mem": "64G",
                "walltime": "2:00:00",
                "gpus": 1,
                "gpu_type": "a100",
            },
            "env_group": "dl",
        },
        {
            "name": "eval",
            "run": "python -m myexp.eval",
            "depends_on": ["fit"],
            "resources": {"cpus": 4, "mem": "16G", "walltime": "0:30:00"},
        },
    ]
