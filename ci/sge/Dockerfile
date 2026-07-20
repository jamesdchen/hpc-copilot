# Single-node SGE (Son of Grid Engine) + sshd container for the
# scheduler-integration CI — the SGE twin of ci/slurm/.
#
# Base-image choice: ubuntu:24.04 + the DISTRO gridengine-* packages. noble,
# NOT jammy. The LP #1774302 SIGSEGV family follows the gridengine 8.1.9
# spool tooling on BOTH: spooldefaults.bin dies rc=139 even with the jemalloc
# preload the LP workaround prescribes (lane runs 29679768215/29701129895/
# 29704323125/29708199144 — the last ON noble with LD_PRELOAD proven in the
# crashing process's env, so "the fix landed in Debian's -9 revision" does
# not cover this binary). The lane therefore bypasses the BDB spool tooling
# ENTIRELY: the entrypoint rewrites the cell bootstrap for CLASSIC (flat-
# file) spooling, which needs no spoolinit/spooldefaults at all — the global
# configuration is seeded as the flat file $SGE_CELL/common/configuration and
# every other object is created through qconf on the running qmaster. Those
# are Debian's Son-of-Grid-Engine build (8.1.9+dfsg, noble universe) — the
# closest thing to a vanilla Son-of-Grid-Engine install that ships as a
# maintained package. There is no maintained single-container SGE/UGE image
# comparable to the Slurm ones (HPC Gridware's Open Cluster Scheduler, the
# maintained successor, publishes a multi-node docker-compose recipe, not a
# single image), so — exactly as the slurm lane did with slurm-wlm — the
# scheduler is stood up from distro packages, no custom build. What this lane
# tests is the SGE DIALECT (qsub -t n-m, qstat -u, qacct, -pe shared, the
# login-shell PATH chain), which is identical across SoGE/UGE/OCS and is
# spooling-method-agnostic. Per §6 of docs/plans/sandbox-proving-run-2026-07-18.md
# it can NEVER cover a specific site's login profile (hoffman2's exact PATH
# shape) — do not oversell it.
#
# The freshly-built hpc-agent wheel is NOT baked in — the workflow pip-installs
# it into this image's python3 at runtime (mirroring the real cluster-env
# convention where hpc_agent lives in the job/login env), so the image layer
# cache stays stable across code changes.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# noble's python3.12 is PEP-668 externally-managed; the workflow pip-installs
# the freshly-built wheel into this image's python at runtime (same contract
# as the slurm lane) — allow it without --break-system-packages on the CLI.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Never start services during the image build: the gridengine postinst would
# otherwise launch sge_qmaster against the BUILD container's random hostname
# and bake that identity into the cell spool. Daemons start in the entrypoint,
# against the runtime hostname.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d

# Preseed the gridengine debconf answers so the install stays non-interactive.
# shared/gridengineconfig is FALSE: with `true` the postinst runs init_cluster
# at image-BUILD time, and its spooldefaults.bin SIGSEGVs inside the buildkit
# sandbox (LP #1774302 family — the build hostname canonicalizes to localhost;
# observed on the lane's first live run, gh run 29679768215). The cell is
# initialized at RUNTIME by the entrypoint instead — and, because the same
# spooldefaults crash reproduces at runtime on noble even with the jemalloc
# preload (gh run 29708199144), the entrypoint seeds a CLASSIC-spooling cell
# by hand (flat files + qconf), never touching the BDB spool tools.
RUN echo "gridengine-master shared/gridenginemaster string sgeci" | debconf-set-selections \
    && echo "gridengine-master shared/gridenginecell string default" | debconf-set-selections \
    && echo "gridengine-master shared/gridengineconfig boolean false" | debconf-set-selections

# gridengine-{master,exec,client}: sge_qmaster + sge_execd + the client tools
# (qsub/qstat/qacct/qconf/qdel). openssh-server: the login node the framework
# SSHes to. rsync + tar: the transport push paths. python3 + pip: dispatcher/
# reporter + the wheel install. python-is-python3: the status reporter's
# remote command runs bare ``python`` when no conda/module activation supplies
# one (cluster_status._ENV_PYTHON) — same rationale as ci/slurm/Dockerfile.
# libjemalloc2: preloaded around the two daemon starts in the entrypoint
# (insurance against the LP #1774302 TLS SIGSEGV family hitting sge_qmaster /
# sge_execd themselves); it otherwise arrives only transitively, and the
# preload must not depend on a transitive accident.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gridengine-master \
        gridengine-exec \
        gridengine-client \
        libjemalloc2 \
        openssh-server \
        rsync \
        tar \
        python3 \
        python3-pip \
        python-is-python3 \
        procps \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- The login-shell PATH dialect (the whole reason this lane exists) -------
# Run-15 finding F7: on real SGE sites (Hoffman2/UGE, CARC, ...) the scheduler
# binaries resolve ONLY through the login profile chain (/etc/profile ->
# /etc/profile.d/*.sh); a NON-login ``ssh host qstat`` is rc 127 there. That
# is exactly why the framework wraps every remote scheduler query in
# ``bash -lc`` (infra/backends/_engine.py::_login_ack / _login). Debian,
# however, FHS-installs the clients into /usr/bin — reachable from EVERY
# shell — which would sanitize PATH the way hoffman2 doesn't and make the
# lane worthless: a regression that dropped the login wrap would stay green.
# So the clients are RELOCATED to a vanilla $SGE_ROOT/bin/$ARCH-style dir and
# exposed ONLY via /etc/profile.d/sge.sh:
#   * login shell (``bash -lc`` / ssh login session): qsub/qstat/qacct resolve
#   * non-login ssh exec channel:                     rc 127 — the Hoffman2 dialect
# Deliberate consequence (mirrors hoffman2, where the reporter's channel is
# equally non-login): the cluster-side status reporter's bare ``qstat`` /
# ``qacct`` subprocess (infra/backends/query.py::query_sge) does NOT resolve
# here, and the reporter falls back to file-based completion detection — the
# framework's real degradation path on an SGE site, pinned by this lane.
RUN install -d -m 0755 /opt/sge/bin/lx-amd64 \
    && for b in qsub qstat qacct qhost qdel qalter qhold qrls qmod qconf \
                qlogin qsh qrsh qmake qquota qrsub qrdel qping; do \
         if [ -e "/usr/bin/$b" ]; then \
           cp -L "/usr/bin/$b" "/opt/sge/bin/lx-amd64/$b" \
           && rm "/usr/bin/$b"; \
         fi; \
       done \
    && printf '%s\n' \
       '# SGE login-shell PATH hook — the Hoffman2/UGE dialect (see ci/sge/Dockerfile).' \
       '# The scheduler binaries resolve ONLY via the login profile chain; a non-login' \
       '# ssh exec gets rc 127, which is what infra/backends/_engine.py::_login_ack' \
       '# (the bash -lc wrap) exists for. Do NOT add these to the system PATH.' \
       'export SGE_ROOT=/var/lib/gridengine' \
       'export SGE_CELL=default' \
       'export PATH=/opt/sge/bin/lx-amd64:$PATH' \
       > /etc/profile.d/sge.sh \
    && chmod 0644 /etc/profile.d/sge.sh \
    && test -x /opt/sge/bin/lx-amd64/qsub \
    && test -x /opt/sge/bin/lx-amd64/qstat \
    && test -x /opt/sge/bin/lx-amd64/qacct \
    && test -x /opt/sge/bin/lx-amd64/qconf \
    && test -x /opt/sge/bin/lx-amd64/qdel \
    && test ! -e /usr/bin/qsub \
    && test ! -e /usr/bin/qstat

# --- Cell bootstrap templates (the slurm.conf twin) --------------------------
# SGE has no single config file the way Slurm has slurm.conf: the cell is
# assembled by qconf at runtime. These are the template inputs the entrypoint
# applies (host group / queue / parallel environment / exec host). No comments
# inside the templates themselves — qconf's file parser is not guaranteed to
# skip them.
COPY ci/sge/qconf /etc/sge-bootstrap
# Strip any CRLF a Windows-side checkout may have introduced (a `\r` in a qconf
# value poisons the bootstrap) — same defensive move as the entrypoint below.
RUN sed -i 's/\r$//' /etc/sge-bootstrap/* /etc/profile.d/sge.sh

# --- Test user the framework logs in as --------------------------------------
# Home under /home/hpcuser; scratch dir the smoke test stages runs into. SGE's
# default global config (enforce_user auto) auto-creates the scheduler-side
# user object on first qsub, so no qconf user wiring is needed for hpcuser.
RUN useradd -m -s /bin/bash hpcuser \
    && install -d -o hpcuser -g hpcuser -m 0700 /home/hpcuser/.ssh \
    && install -d -o hpcuser -g hpcuser -m 0755 /home/hpcuser/scratch

# --- sshd runtime dir ---
RUN install -d -m 0755 /run/sshd

COPY ci/sge/entrypoint.sh /usr/local/bin/entrypoint.sh
# Strip any CRLF a Windows-side checkout may have introduced (a `\r` in the
# shebang line breaks exec) and make it executable.
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 22

# The entrypoint fills authorized_keys from /pubkey (bind-mounted or docker-cp'd
# by the workflow), starts sge_qmaster + bootstraps the cell + starts sge_execd
# + sshd, then blocks.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
