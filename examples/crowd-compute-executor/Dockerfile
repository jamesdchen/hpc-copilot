# Containerized hpc-agent executor — the unit every crowd-compute
# platform (Vast.ai / SaladCloud / Akash) accepts, and runnable
# unchanged under a cluster dispatcher via Apptainer.
#
# The image carries NO hpc-agent dependency: the executor speaks only
# the dispatcher env contract. The launcher (cluster dispatcher or
# platform-side wrapper) supplies:
#
#   HPC_TASK_ID    0-based task index
#   RESULT_DIR     output directory (mount a volume here, or have the
#                  platform wrapper upload it after exit)
#   HPC_KW_<KEY>   JSON-encoded kwargs from tasks.resolve(task_id)
#
# Build:  docker build -t crowd-executor examples/crowd-compute-executor
# Smoke:  docker run -e HPC_TASK_ID=0 -e RESULT_DIR=/out \
#             -e HPC_KW_N_SAMPLES=50000 -v "$PWD/out:/out" crowd-executor
FROM python:3.12-slim

WORKDIR /app
COPY executor.py .

ENTRYPOINT ["python", "/app/executor.py"]
