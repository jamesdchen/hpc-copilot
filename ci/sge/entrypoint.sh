#!/bin/bash
# Bring up the single-node SGE login node: authorized_keys, sge_qmaster, the
# cell bootstrap (admin/submit/exec host + @allhosts + all.q + shared PE),
# sge_execd, sshd. Then block so the container stays up for the workflow to
# exec into (pip-install the wheel) and for the test driver to SSH into.
#
# Mirror of ci/slurm/entrypoint.sh. The slurm.conf twin: SGE has no single
# config file — the cell is assembled by qconf from the templates in
# /etc/sge-bootstrap/ (ci/sge/qconf/).
#
# Deliberately NOT `set -e` on the daemon/qconf starts — we want a clear log
# and a live container for diagnosis if a step fails, matching the slurm lane.
set -uo pipefail

log() { printf '[entrypoint] %s\n' "$*"; }

# Container-admin PATH for THIS script. The login-shell dialect
# (/etc/profile.d/sge.sh) governs SSH sessions, not the entrypoint; the
# entrypoint is the cluster administrator and sets SGE_ROOT + the relocated
# bin dir explicitly. /usr/lib/gridengine carries the daemons (sge_qmaster /
# sge_execd), started directly below.
export SGE_ROOT=/var/lib/gridengine
export SGE_CELL=default
export PATH=/opt/sge/bin/lx-amd64:/usr/lib/gridengine:$PATH
CELL_COMMON="$SGE_ROOT/$SGE_CELL/common"
SPOOL=/var/spool/gridengine
QMASTER_SPOOL="$SPOOL/qmaster"
EXECD_SPOOL="$SPOOL/execd"
HN="$(hostname)"

# jemalloc preload for the two daemon starts: the LP #1774302 family of
# glibc-TLS SIGSEGVs hits gridengine binaries on Ubuntu's build; the preload
# is cheap insurance for the daemons. (It did NOT cure spooldefaults.bin —
# see the classic-spooling block below — which is why no spool tool runs here
# at all.) Resolved once; an empty value is a harmless no-op preload.
JEMALLOC="$(ls /usr/lib/x86_64-linux-gnu/libjemalloc.so.* 2>/dev/null | head -n 1 || true)"
log "jemalloc preload for daemons: ${JEMALLOC:-none found}"

# --- authorized_keys for the test user ---------------------------------------
# Identical contract to ci/slurm/entrypoint.sh: the workflow provides the
# PUBLIC half of a throwaway keypair at /pubkey (bind-mount or docker cp).
if [ -f /pubkey ]; then
    install -d -o hpcuser -g hpcuser -m 0700 /home/hpcuser/.ssh
    cp /pubkey /home/hpcuser/.ssh/authorized_keys
    chown hpcuser:hpcuser /home/hpcuser/.ssh/authorized_keys
    chmod 0600 /home/hpcuser/.ssh/authorized_keys
    log "installed authorized_keys for hpcuser"
else
    log "WARNING: /pubkey not found; hpcuser has no authorized_keys (SSH will fail)"
fi

# --- hostname resolution ------------------------------------------------------
# qmaster/execd insist the local hostname resolves. Docker wires /etc/hosts
# itself for --hostname; backstop it for other runtimes.
getent hosts "$HN" >/dev/null 2>&1 || printf '127.0.1.1\t%s\n' "$HN" >> /etc/hosts

# --- runtime cell init: CLASSIC spooling, seeded by hand ----------------------
# WHY NOT init_cluster / Berkeley-DB: Debian's init_cluster is BDB-only. On
# noble its spoolinit step seeds the spooldb fine, but spooldefaults.bin then
# SIGSEGVs EVEN WITH the jemalloc preload (gh run 29708199144, sh -x trace:
# "Segmentation fault (core dumped)" immediately after
# `spooldefaults configuration /usr/share/gridengine/default-configuration`,
# LD_PRELOAD proven in its env — the LP #1774302 jemalloc cure does not cover
# this binary). init_cluster runs set -e, so it dies there: the spooldb is
# left WITHOUT a configuration object, and qmaster then exits "global
# configuration not defined" before ever opening a messages file (run
# 29708199144: no qmaster messages file existed at diagnostics time).
#
# Classic spooling needs NO spool binaries: the spool is flat files. The
# global configuration IS $SGE_CELL/common/configuration (SoGE source,
# libs/spool/flatfile: SGE_TYPE_CONFIG resolves to <common_dir>/configuration);
# every other object (managers, centry, cqueues, exec_hosts, ...) lives under
# the qmaster spool dir, which qmaster maintains itself — it even auto-adds
# root as a manager at startup (daemons/qmaster/setup_qmaster.c), which is
# exactly the user running qconf below. The SGE dialect this lane exists to
# exercise (qsub -t, qstat, qacct, -pe shared, the login-shell PATH chain) is
# spooling-agnostic, so nothing about the coverage changes.
#
# bootstrap: the packaged one selects BDB (spooling_params
# /var/spool/gridengine/spooldb). Rewrite it wholesale for classic — hermetic
# against packaging drift. For classic, spooling_params is
# "<common_dir>;<qmaster_spool_dir>" (sge_bootstrap(5)).
install -d -o sgeadmin -g sgeadmin -m 0755 "$CELL_COMMON"
cat > "$CELL_COMMON/bootstrap" <<EOF
# Classic-spooling bootstrap, written by ci/sge/entrypoint.sh. The packaged
# BDB bootstrap is replaced wholesale — see the block comment above. Field
# values otherwise mirror the package's default-bootstrap.
admin_user              sgeadmin
default_domain          none
ignore_fqdn             false
spooling_method         classic
spooling_lib            libspoolc
spooling_params         $CELL_COMMON;$QMASTER_SPOOL
binary_path             /usr/sbin
qmaster_spool_dir       $QMASTER_SPOOL
security_mode           none
listener_threads        2
worker_threads          2
scheduler_threads       1
EOF

# The global configuration. With classic spooling this text file IS the
# authoritative object (qconf -mconf rewrites it in place), not a paper-over
# of a spooldb copy. The package ships a pristine default; seed only when
# absent so a container restart keeps any runtime qconf edits.
if [ ! -s "$CELL_COMMON/configuration" ]; then
    install -o sgeadmin -g sgeadmin -m 0644 \
        /usr/share/gridengine/default-configuration "$CELL_COMMON/configuration" \
        && log "seeded global configuration (classic spool: $CELL_COMMON/configuration)"
fi

# Point the cell at the RUNTIME hostname unconditionally — cheap and idempotent,
# and it keeps the container honest if it is ever run under a different
# --hostname than the debconf-preseeded 'sgeci'.
printf '%s\n' "$HN" > "$CELL_COMMON/act_qmaster"

# The daemons setuid to sgeadmin (the install-time admin user) regardless of
# who launches them, and qmaster must WRITE act_qmaster, the spooled objects
# (managers/centry/cqueues/exec_hosts/... under the qmaster spool dir),
# accounting under common/, and the messages log — a root-owned tree is a
# fatal EACCES loop (gh run 29701129895: "can't open .../act_qmaster for
# writing qmaster hostname: Permission denied", qmaster never binds 6444).
install -d -o sgeadmin -g sgeadmin -m 0755 \
    "$QMASTER_SPOOL" "$QMASTER_SPOOL/job_scripts" "$EXECD_SPOOL"
chown -R sgeadmin:sgeadmin "$SGE_ROOT/$SGE_CELL" "$SPOOL"

# --- sge_qmaster ---------------------------------------------------------------
# Started DIRECTLY, never via /etc/init.d/gridengine-master: that init script
# is silent under the container's VERBOSE=no and swallowed every failure in
# the failing runs (gh run 29708199144: zero output between "starting
# sge_qmaster" and "init script left no qmaster running"). Direct start +
# captured stderr + a loud readiness gate makes the next failure
# self-diagnosing from `docker logs sgeci` alone.
QMASTER_START_LOG="$QMASTER_SPOOL/qmaster.start.log"
if pgrep -x sge_qmaster >/dev/null 2>&1; then
    log "sge_qmaster already running"
else
    log "starting sge_qmaster (classic spooling)"
    LD_PRELOAD="$JEMALLOC" /usr/lib/gridengine/sge_qmaster >"$QMASTER_START_LOG" 2>&1
    log "sge_qmaster launch rc=$? (start log: $QMASTER_START_LOG)"
    [ -s "$QMASTER_START_LOG" ] && sed 's/^/[qmaster] /' "$QMASTER_START_LOG" | tail -n 20
fi

# --- wait for qmaster (loud gate) ----------------------------------------------
qmaster_up=0
for i in $(seq 1 30); do
    if qconf -sh >/dev/null 2>&1; then qmaster_up=1; break; fi
    log "qmaster not answering qconf yet (attempt $i)"
    sleep 2
done

if [ "$qmaster_up" != 1 ]; then
    log "ERROR: qmaster never answered qconf within 60s — its fate follows"
    ps -ef | grep -i '[s]ge_' || true
    if [ -s "$QMASTER_START_LOG" ]; then
        log "qmaster start log:"
        sed 's/^/[qmaster] /' "$QMASTER_START_LOG" | tail -n 40
    fi
    if [ -f "$QMASTER_SPOOL/messages" ]; then
        log "qmaster messages tail:"
        tail -n 40 "$QMASTER_SPOOL/messages" | sed 's/^/[qmaster:messages] /'
    else
        log "qmaster wrote no messages file (died before opening it)"
    fi
    log "cell common: $(ls "$CELL_COMMON" 2>/dev/null || echo MISSING)"
    log "qmaster spool: $(ls "$QMASTER_SPOOL" 2>/dev/null || echo MISSING)"
    # Fall through on purpose: the qconf mutations below will fail (|| true
    # keeps the posture), sshd still starts so the failure stays diagnosable,
    # and the workflow's readiness probe fails the lane.
else
    log "qmaster answering qconf"
fi

# --- cell bootstrap (idempotent) ----------------------------------------------
# admin + submit host entries for the runtime hostname.
qconf -sh 2>/dev/null | grep -qx "$HN" || qconf -ah "$HN" || true
qconf -ss 2>/dev/null | grep -qx "$HN" || qconf -as "$HN" || true

# exec host entry — qmaster must know the host before sge_execd may register.
if ! qconf -sel 2>/dev/null | grep -qx "$HN"; then
    sed "s/__HOSTNAME__/$HN/" /etc/sge-bootstrap/exec_host.tmpl > /tmp/exec_host.tmpl
    qconf -Ae /tmp/exec_host.tmpl || true
fi

# @allhosts must name the RUNTIME host (a postinst-created group may carry a
# build-time one): modify when present, add when absent.
sed "s/__HOSTNAME__/$HN/" /etc/sge-bootstrap/allhosts.grp > /tmp/allhosts.grp
if qconf -shgrp @allhosts >/dev/null 2>&1; then
    qconf -Mhgrp /tmp/allhosts.grp || true
else
    qconf -Ahgrp /tmp/allhosts.grp || true
fi

# Default complexes — spooldefaults' other load-bearing role, done through the
# RUNNING qmaster (the file format is the same qconf one). The complexes are
# REQUIRED: the framework submits -l h_rt= / -l h_data=
# (infra/backends/_engine.py), which qmaster rejects when the complex is
# unknown. `qconf -Mc` overwrites the whole complex configuration from the
# file (qconf(1)), so it is idempotent across container restarts.
if [ -f /usr/share/gridengine/util/resources/centry ]; then
    qconf -Mc /usr/share/gridengine/util/resources/centry >/tmp/centry.log 2>&1
    crc=$?
    if [ "$crc" -eq 0 ]; then
        log "seeded default complexes (qconf -Mc)"
    else
        log "WARNING: complex seed failed (rc=$crc) — -l h_rt/h_data submits will break:"
        sed 's/^/    /' /tmp/centry.log | tail -n 15
    fi
else
    log "WARNING: /usr/share/gridengine/util/resources/centry missing — -l h_rt/h_data submits will break"
fi
# Default usersets — parity with init_cluster. Not load-bearing for the smoke
# (nothing references ACLs), so a failure here is silent-by-design.
if [ -f /usr/share/gridengine/util/resources/usersets ]; then
    qconf -Mu /usr/share/gridengine/util/resources/usersets >/dev/null 2>&1 \
        || qconf -Au /usr/share/gridengine/util/resources/usersets >/dev/null 2>&1 \
        || true
fi

# Parallel environment 'shared' — the framework's SGE cpu path requests
# ``-pe shared N`` for cpus>0 (infra/backends/_engine.py::_build_resource_flags).
qconf -sp shared >/dev/null 2>&1 || qconf -Ap /etc/sge-bootstrap/shared.pe || true

# all.q — modify when present (a postinst-created queue may carry build-host
# state), add when absent. The queue template deliberately sets
# load_thresholds NONE: a container shares the CI runner's kernel, so the
# host's load average can sit above any realistic np_load_avg threshold and
# alarm the queue ('a' state), pending every job — the SGE analogue of the
# slurm lane's deliberate resource under-reporting.
if qconf -sq all.q >/dev/null 2>&1; then
    qconf -Mq /etc/sge-bootstrap/all.q || true
else
    qconf -Aq /etc/sge-bootstrap/all.q || true
fi

# --- sge_execd -----------------------------------------------------------------
# Direct start for the same reason as qmaster (the init script is silent and
# swallowed failures). execd fetches its configuration from qmaster over TCP;
# with qmaster answering, registration follows on its own.
EXECD_START_LOG="$EXECD_SPOOL/execd.start.log"
if pgrep -x sge_execd >/dev/null 2>&1; then
    log "sge_execd already running"
else
    log "starting sge_execd"
    LD_PRELOAD="$JEMALLOC" /usr/lib/gridengine/sge_execd >"$EXECD_START_LOG" 2>&1
    log "sge_execd launch rc=$? (start log: $EXECD_START_LOG)"
    [ -s "$EXECD_START_LOG" ] && sed 's/^/[execd] /' "$EXECD_START_LOG" | tail -n 20
fi

log "cluster state:"
qstat -f || true

# --- sshd (foreground) ---------------------------------------------------------
# Generate host keys if the image was built without them, then run sshd in the
# foreground so it becomes the container's blocking PID.
ssh-keygen -A >/dev/null 2>&1 || true
log "starting sshd (foreground)"
exec /usr/sbin/sshd -D -e
