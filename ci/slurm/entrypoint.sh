#!/bin/bash
# Bring up the single-node Slurm login node: authorized_keys, munge, slurmctld,
# slurmd, sshd. Then block so the container stays up for the workflow to exec
# into (pip-install the wheel) and for the test driver to SSH into.
#
# Deliberately NOT `set -e` on the daemon starts — we want to report a clear
# failure if a daemon dies, and to keep the container alive for diagnosis.
set -uo pipefail

log() { printf '[entrypoint] %s\n' "$*"; }

# --- authorized_keys for the test user ---------------------------------------
# The workflow generates a throwaway keypair and provides the PUBLIC half at
# /pubkey (bind-mount or docker cp). Copy it into place with the perms sshd
# demands. Absent /pubkey is fatal — without it the test driver can't log in.
if [ -f /pubkey ]; then
    install -d -o hpcuser -g hpcuser -m 0700 /home/hpcuser/.ssh
    cp /pubkey /home/hpcuser/.ssh/authorized_keys
    chown hpcuser:hpcuser /home/hpcuser/.ssh/authorized_keys
    chmod 0600 /home/hpcuser/.ssh/authorized_keys
    log "installed authorized_keys for hpcuser"
else
    log "WARNING: /pubkey not found; hpcuser has no authorized_keys (SSH will fail)"
fi

# --- munge -------------------------------------------------------------------
log "starting munge"
install -d -o munge -g munge -m 0755 /run/munge
runuser -u munge -- /usr/sbin/munged --force
sleep 1

# --- slurm daemons -----------------------------------------------------------
# slurmctld (controller) first, then slurmd (compute). Both foreground-fork by
# default; run them backgrounded and check they registered.
log "starting slurmctld"
/usr/sbin/slurmctld
log "starting slurmd"
/usr/sbin/slurmd
sleep 2

# Nudge the node into service in case it registered before the ctld was ready.
scontrol update NodeName=localhost State=RESUME 2>/dev/null || true

if command -v sinfo >/dev/null 2>&1; then
    log "cluster state:"
    sinfo || true
fi

# --- sshd (foreground) -------------------------------------------------------
# Generate host keys if the image was built without them, then run sshd in the
# foreground so it becomes the container's blocking PID.
ssh-keygen -A >/dev/null 2>&1 || true
log "starting sshd (foreground)"
exec /usr/sbin/sshd -D -e
