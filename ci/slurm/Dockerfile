# Single-node Slurm + sshd container for the scheduler-integration CI.
#
# Pragmatic v1 (docs/internals/scheduler-integration-ci.md): slurmctld + slurmd
# + munge + sshd all in ONE container. Ubuntu's packaged slurm-wlm is the
# simplest thing that yields working sbatch/squeue/sacct with the least moving
# parts — no custom Slurm build, no accounting DB, no cgroups.
#
# The freshly-built hpc-agent wheel is NOT baked in — the workflow pip-installs
# it into this image's python3 at runtime (mirroring the real cluster-env
# convention where hpc_agent lives in the job/login env), so the image layer
# cache stays stable across code changes.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# slurm-wlm: slurmctld + slurmd + the client tools (sbatch/squeue/sacct).
# munge: Slurm's auth. openssh-server: the login node the framework SSHes to.
# rsync + tar: the transport push paths. python3 + pip: dispatcher/reporter +
# the wheel install. gettext-base/procps/sudo: small runtime helpers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        slurm-wlm \
        slurm-client \
        munge \
        openssh-server \
        rsync \
        tar \
        python3 \
        python3-pip \
        procps \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Munge key (deterministic; this is a throwaway CI auth domain) ---
# A single-node auth domain needs one key present with tight perms. Generate a
# fixed-size key at build so slurmctld and slurmd agree.
RUN install -d -o munge -g munge -m 0700 /etc/munge /var/lib/munge /var/log/munge /run/munge \
    && dd if=/dev/urandom bs=1 count=1024 of=/etc/munge/munge.key 2>/dev/null \
    && chown munge:munge /etc/munge/munge.key \
    && chmod 0400 /etc/munge/munge.key

# --- Slurm spool / log dirs (root-owned; SlurmUser=root in slurm.conf) ---
RUN install -d -o root -g root -m 0755 \
        /var/spool/slurmctld /var/spool/slurmd /var/log/slurm \
    && install -d -m 0755 /run

# --- Slurm config (single-node; see ci/slurm/slurm.conf) ---
COPY ci/slurm/slurm.conf /etc/slurm/slurm.conf
# Older tooling still looks under /etc/slurm-llnl; symlink so both resolve.
RUN ln -sf /etc/slurm /etc/slurm-llnl

# --- Test user the framework logs in as ---
# Home under /home/hpcuser; scratch dir the smoke test stages runs into.
RUN useradd -m -s /bin/bash hpcuser \
    && install -d -o hpcuser -g hpcuser -m 0700 /home/hpcuser/.ssh \
    && install -d -o hpcuser -g hpcuser -m 0755 /home/hpcuser/scratch

# --- sshd runtime dir ---
RUN install -d -m 0755 /run/sshd

COPY ci/slurm/entrypoint.sh /usr/local/bin/entrypoint.sh
# Strip any CRLF a Windows-side checkout may have introduced (a `\r` in the
# shebang line breaks exec) and make it executable.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 22

# The entrypoint fills authorized_keys from /pubkey (bind-mounted or docker-cp'd
# by the workflow), starts munge + slurmctld + slurmd + sshd, then blocks.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
