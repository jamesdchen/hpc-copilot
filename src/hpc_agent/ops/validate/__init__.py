"""hpc_agent.ops.validate — pre-submit input validators.

Each submodule registers a ``validate-X`` primitive that the
``validate-campaign`` workflow composes. All validators are pure-local
— the caller passes any SSH-bound data in, so the framework boundary
stays side-effect-free.

Validators (all primitives keep their existing ``validate-*`` wire name):

* :mod:`hpc_agent.ops.validate.executor_signatures`
* :mod:`hpc_agent.ops.validate.input_dataset`
* :mod:`hpc_agent.ops.validate.parents_ready`
* :mod:`hpc_agent.ops.validate.self_qos_limit`
* :mod:`hpc_agent.ops.validate.stochastic_marker`
* :mod:`hpc_agent.ops.validate.walltime_against_history`

Eager re-exports are deliberately avoided.
"""
