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
# sge_execd) as a fallback to the init scripts.
export SGE_ROOT=/var/lib/gridengine
export SGE_CELL=default
export PATH=/opt/sge/bin/lx-amd64:/usr/lib/gridengine:$PATH
CELL_COMMON="$SGE_ROOT/$SGE_CELL/common"
HN="$(hostname)"

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

# --- runtime cell spool init ---------------------------------------------------
# The build-time postinst init is disabled (shared/gridengineconfig=false —
# spooldefaults.bin SIGSEGVs in the buildkit sandbox, LP #1774302 family). Init
# here instead, at runtime. The crash is the gridengine/jemalloc initial-exec
# TLS fault: rc=139 confirmed at BOTH build and runtime (run 29703965543),
# dying right after "Initializing spool" and leaving common/ with only
# 'bootstrap' — qmaster then fails "global configuration not defined / setup
# failed". Workaround: PRELOAD jemalloc so it owns malloc from process start,
# and sh -x the wrapper so a remaining crash names its exact failing binary.
if [ ! -e /var/spool/gridengine/spooldb/sge ]; then
    JEMALLOC="$(ls /usr/lib/x86_64-linux-gnu/libjemalloc.so.* 2>/dev/null | head -n 1 || true)"
    log "initializing cluster spool (init_cluster); jemalloc: ${JEMALLOC:-none found}"
    su -s /bin/sh -c "LD_PRELOAD='$JEMALLOC' sh -x /usr/share/gridengine/scripts/init_cluster $SGE_ROOT $SGE_CELL /var/spool/gridengine/spooldb sgeadmin" sgeadmin 2>&1 | tail -n 80 \
        || log "init_cluster rc=${PIPESTATUS[0]} (continuing; backfill below)"
fi
log "cell common after init: $(ls "$CELL_COMMON" 2>/dev/null || echo MISSING)"
log "spooldb after init: $(ls /var/spool/gridengine/spooldb 2>/dev/null || echo MISSING)"
# Backfill the textual global configuration if the spool seeding died before
# writing it. (With BDB spooling the authoritative copy lives in the spooldb
# that spooldefaults seeds — this only papers over the text side; the preload
# above is the real fix.) The package ships a pristine default.
if [ ! -s "$CELL_COMMON/configuration" ]; then
    for src in /usr/share/gridengine/default-configuration /var/lib/gridengine/default-configuration; do
        if [ -f "$src" ]; then
            install -o sgeadmin -g sgeadmin -m 0644 "$src" "$CELL_COMMON/configuration" \
                && log "backfilled global configuration from $src"
            break
        fi
    done
fi

# --- cell identity ------------------------------------------------------------
# Point the cell at the RUNTIME hostname unconditionally — cheap and idempotent,
# and it keeps the container honest if it is ever run under a different
# --hostname than the debconf-preseeded 'sgeci'.
if [ -d "$CELL_COMMON" ]; then
    printf '%s\n' "$HN" > "$CELL_COMMON/act_qmaster"
    # The daemons setuid to sgeadmin (the install-time admin user) regardless
    # of who launches them. With the build-time postinst disabled the cell
    # tree is still root-owned, and qmaster must WRITE act_qmaster and create
    # accounting under common/ at startup — a root-owned cell is a fatal
    # EACCES loop (gh run 29701129895: "can't open .../act_qmaster for
    # writing qmaster hostname: Permission denied", qmaster never binds 6444).
    chown -R sgeadmin:sgeadmin "$SGE_ROOT/$SGE_CELL"
else
    log "WARNING: $CELL_COMMON missing — the gridengine postinst did not lay down the cell"
fi

# --- sge_qmaster ---------------------------------------------------------------
log "starting sge_qmaster"
/etc/init.d/gridengine-master start || true
sleep 2
if ! pgrep -x sge_qmaster >/dev/null 2>&1; then
    log "init script left no qmaster running; starting sge_qmaster directly"
    /usr/lib/gridengine/sge_qmaster
    sleep 2
fi

# --- cell bootstrap (idempotent) ----------------------------------------------
# Wait for qmaster to answer qconf before mutating the cell.
for i in $(seq 1 15); do
    if qconf -sh >/dev/null 2>&1; then break; fi
    log "qmaster not answering qconf yet (attempt $i)"
    sleep 1
done

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
log "starting sge_execd"
/etc/init.d/gridengine-exec start || true
sleep 2
if ! pgrep -x sge_execd >/dev/null 2>&1; then
    log "init script left no execd running; starting sge_execd directly"
    /usr/lib/gridengine/sge_execd
    sleep 2
fi

log "cluster state:"
qstat -f || true

# --- sshd (foreground) ---------------------------------------------------------
# Generate host keys if the image was built without them, then run sshd in the
# foreground so it becomes the container's blocking PID.
ssh-keygen -A >/dev/null 2>&1 || true
log "starting sshd (foreground)"
exec /usr/sbin/sshd -D -e
