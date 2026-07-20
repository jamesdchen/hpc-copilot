#!/bin/bash
# Hermetic test of the qmaster fate dump in ../entrypoint.sh.
#
# The dump functions (dump_one_spool_file / dump_qmaster_fate) are EXTRACTED
# from the real entrypoint.sh by line range below — never copied — so this
# test cannot drift from the shipped code. Each scenario builds a fake
# $SPOOL/$CELL_COMMON tree under a mktemp dir and runs the extracted code
# against it with stubbed qconf/strace/pgrep on PATH. No docker, no SGE, no
# root needed — runs in any bash (incl. Git Bash on the Windows dev box).
#
# Scenarios:
#   A  signal-death signature (run 29709733724): rc=0 parent + EMPTY start
#      log + no messages file -> VERDICT narrated, strace retry FIRES, the
#      stubbed trace's SIGSEGV line is dumped, qmaster_up stays 0.
#   B  config-error signature: NON-empty start log + messages present ->
#      tails dumped, NO VERDICT (a printed error is not the signal signature).
#   C  strace unavailable -> the "strace not installed" narration fires.
#   D  pgrep reports a LIVE sge_qmaster -> the strace retry must NOT fire
#      (double-start races the live spool — regression pin for the guard).
#   E  hang-after-open: EMPTY start log but messages EXISTS -> NO VERDICT
#      (the messages tail, not the signal verdict, carries that story).
#   F  qconf fails once then answers -> the re-poll (not a single probe)
#      flips qmaster_up to 1 and the "ANSWERED under strace" line prints.
#
# Exit 0 = all scenarios pass; failures print FAIL lines and exit 1.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$HERE/../entrypoint.sh"
BASE_PATH="$PATH"
fail=0

assert_contains() { # $1=out file, $2=expected fixed string, $3=scenario label
    if ! grep -qF -- "$2" "$1"; then
        echo "FAIL[$3]: expected: $2"
        fail=1
    fi
}
assert_not_contains() { # $1=out file, $2=forbidden fixed string, $3=scenario label
    if grep -qF -- "$2" "$1"; then
        echo "FAIL[$3]: unexpected: $2"
        fail=1
    fi
}
assert_file_absent() { # $1=path, $2=scenario label
    if [ -e "$1" ]; then
        echo "FAIL[$2]: file should not exist: $1"
        fail=1
    fi
}
assert_file_nonempty() { # $1=path, $2=scenario label
    if [ ! -s "$1" ]; then
        echo "FAIL[$2]: file missing or empty: $1"
        fail=1
    fi
}

# run_dump <scenario-dir>: assemble the harness (log stub + optional prelude +
# extracted functions + call) and run it against the scenario's fake tree.
run_dump() {
    scen="$1"
    {
        echo 'set -uo pipefail'
        echo 'log() { printf "[entrypoint] %s\n" "$*"; }'
        [ -f "$scen/prelude.sh" ] && cat "$scen/prelude.sh"
        # Line-range extraction from the REAL entrypoint — cannot drift.
        sed -n '/^dump_one_spool_file()/,/^}/p' "$ENTRYPOINT"
        sed -n '/^dump_qmaster_fate()/,/^}/p' "$ENTRYPOINT"
        echo 'dump_qmaster_fate'
        echo 'echo "HARNESS-END qmaster_up=${qmaster_up:-0}"'
    } > "$scen/harness.sh"
    SGE_ROOT=/var/lib/gridengine SGE_CELL=default \
    CELL_COMMON="$scen/cell" SPOOL="$scen/spool" \
    QMASTER_SPOOL="$scen/spool/qmaster" EXECD_SPOOL="$scen/spool/execd" \
    QMASTER_START_LOG="$scen/spool/qmaster/qmaster.start.log" \
    HN=sgeci JEMALLOC='' qmaster_up=0 HPC_DUMP_TEST_DIR="$scen" \
    PATH="$scen/bin:$BASE_PATH" bash "$scen/harness.sh" > "$scen/out.txt" 2>&1
}

mk_tree() { # $1=scenario dir: the common fake cell/spool skeleton
    scen="$1"
    mkdir -p "$scen/spool/qmaster" "$scen/spool/execd" "$scen/cell" "$scen/bin"
    printf 'sgeci\n' > "$scen/cell/act_qmaster"
    printf 'spooling_method         classic\n' > "$scen/cell/bootstrap"
}

mk_qconf_never() { # stub qconf: never answers
    cat > "$1/bin/qconf" <<'EOF'
#!/bin/bash
exit 1
EOF
    chmod +x "$1/bin/qconf"
}

mk_strace_segv() { # stub strace: writes a fake trace ending in SIGSEGV, exits 1
    cat > "$1/bin/strace" <<'EOF'
#!/bin/bash
out=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done
printf '12345 openat(AT_FDCWD, "/var/lib/gridengine/default/common/bootstrap", O_RDONLY) = 3\n12345 +++ killed by SIGSEGV (core dumped) +++\n' > "$out"
exit 1
EOF
    chmod +x "$1/bin/strace"
}

# --- Scenario A: signal-death signature -> VERDICT + strace retry fires ------
scen="$(mktemp -d /tmp/sge-dump-A.XXXXXX)"; mk_tree "$scen"
: > "$scen/spool/qmaster/qmaster.start.log"
printf 'error: can'"'"'t find connection\n' > "$scen/spool/execd/execd.start.log"
mk_qconf_never "$scen"; mk_strace_segv "$scen"
run_dump "$scen"
assert_contains "$scen/out.txt" 'VERDICT: launch rc=0 + EMPTY start log + no messages file = SIGNAL-DEATH' A
assert_contains "$scen/out.txt" 'MISSING — qmaster died before opening its own log' A
assert_contains "$scen/out.txt" 'EXISTS but is EMPTY (0 bytes)' A
assert_contains "$scen/out.txt" 'retrying qmaster under strace' A
assert_contains "$scen/out.txt" 'killed by SIGSEGV' A
assert_contains "$scen/out.txt" 'HARNESS-END qmaster_up=0' A
assert_file_nonempty "$scen/spool/qmaster/qmaster.strace.log" A

# --- Scenario B: config-error signature -> tails, NO VERDICT -----------------
scen="$(mktemp -d /tmp/sge-dump-B.XXXXXX)"; mk_tree "$scen"
printf 'critical error: qmaster hostname mismatch\n' > "$scen/spool/qmaster/qmaster.start.log"
printf '07/19/2026 00:00:01|main|sgeci|I|starting up\n' > "$scen/spool/qmaster/messages"
mk_qconf_never "$scen"; mk_strace_segv "$scen"
run_dump "$scen"
assert_not_contains "$scen/out.txt" 'VERDICT' B
assert_contains "$scen/out.txt" 'qmaster hostname mismatch' B
assert_contains "$scen/out.txt" 'starting up' B

# --- Scenario C: no strace available -> narration ----------------------------
scen="$(mktemp -d /tmp/sge-dump-C.XXXXXX)"; mk_tree "$scen"
: > "$scen/spool/qmaster/qmaster.start.log"
mk_qconf_never "$scen"
# Shadow `command -v strace` to report absence even where the host has one.
cat > "$scen/prelude.sh" <<'EOF'
command() { if [ "${1:-}" = "-v" ] && [ "${2:-}" = "strace" ]; then return 1; fi; builtin command "$@"; }
EOF
run_dump "$scen"
assert_contains "$scen/out.txt" 'strace not installed' C
assert_not_contains "$scen/out.txt" 'retrying qmaster under strace' C

# --- Scenario D: LIVE qmaster -> strace retry must NOT fire (guard pin) ------
scen="$(mktemp -d /tmp/sge-dump-D.XXXXXX)"; mk_tree "$scen"
: > "$scen/spool/qmaster/qmaster.start.log"
mk_qconf_never "$scen"; mk_strace_segv "$scen"
# pgrep stub: sge_qmaster is ALIVE (pid 4242); everything else absent.
cat > "$scen/bin/pgrep" <<'EOF'
#!/bin/bash
case "$*" in
    "-x sge_qmaster") echo 4242; exit 0;;
    *) exit 1;;
esac
EOF
chmod +x "$scen/bin/pgrep"
run_dump "$scen"
assert_contains "$scen/out.txt" 'NOT retrying under strace' D
assert_not_contains "$scen/out.txt" 'retrying qmaster under strace (bounded' D
assert_file_absent "$scen/spool/qmaster/qmaster.strace.log" D

# --- Scenario E: hang-after-open -> NO VERDICT (messages carries the story) --
scen="$(mktemp -d /tmp/sge-dump-E.XXXXXX)"; mk_tree "$scen"
: > "$scen/spool/qmaster/qmaster.start.log"
printf '07/19/2026 00:00:01|main|sgeci|I|starting up\n' > "$scen/spool/qmaster/messages"
mk_qconf_never "$scen"; mk_strace_segv "$scen"
run_dump "$scen"
assert_not_contains "$scen/out.txt" 'VERDICT' E
assert_contains "$scen/out.txt" 'starting up' E

# --- Scenario F: qconf answers on the 2nd re-poll -> qmaster_up flips to 1 ---
scen="$(mktemp -d /tmp/sge-dump-F.XXXXXX)"; mk_tree "$scen"
: > "$scen/spool/qmaster/qmaster.start.log"
mk_strace_segv "$scen"
cat > "$scen/bin/qconf" <<'EOF'
#!/bin/bash
n=$(cat "$HPC_DUMP_TEST_DIR/n" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$HPC_DUMP_TEST_DIR/n"
[ "$n" -ge 2 ]
EOF
chmod +x "$scen/bin/qconf"
run_dump "$scen"
assert_contains "$scen/out.txt" 'ANSWERED under strace (re-poll' F
assert_contains "$scen/out.txt" 'HARNESS-END qmaster_up=1' F

if [ "$fail" -eq 0 ]; then
    echo "PASS: all dump_qmaster_fate scenarios (A-F)"
else
    echo "FAILURES above — scenario outputs under /tmp/sge-dump-*"
fi
exit "$fail"
