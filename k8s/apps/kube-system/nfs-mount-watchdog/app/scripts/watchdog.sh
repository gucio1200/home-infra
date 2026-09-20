#!/bin/sh
# Unblocks NFS mounting on a node after a NAS swallowed a mount-time RPC
# (e.g. NFSv4 SETCLIENTID sent while the NAS was still booting). The kernel never
# times such a request out while the TCP connection stays up, and every other
# NFS mount on the node queues behind it, so pods hang in Init/ContainerCreating.
#
# Only the kubelet's mount helper process is ever signalled. An established
# mount has no such process, so mounted volumes and their connections cannot be
# affected; the worst case is one aborted mount attempt that the kubelet retries.
set -u

MIN_AGE_SECONDS="${MIN_AGE_SECONDS:-300}"
CONFIRM_SECONDS="${CONFIRM_SECONDS:-120}"
MAX_CTXT_DELTA="${MAX_CTXT_DELTA:-10}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-30}"
DRY_RUN="${DRY_RUN:-false}"
STATE_DIR="${STATE_DIR:-/state}"
# dotfile, so the per-process state cleanup glob never touches it
HEARTBEAT_FILE="$STATE_DIR/.heartbeat"

# liveness probe: the loop must have completed a pass recently
if [ "${1:-}" = "healthcheck" ]; then
  last=$(cat "$HEARTBEAT_FILE" 2>/dev/null)
  [ -n "$last" ] || exit 1
  [ $(( $(date +%s) - last )) -le $(( INTERVAL_SECONDS * 3 + 30 )) ]
  exit
fi

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

heartbeat() {
  date +%s > "$HEARTBEAT_FILE.tmp" && mv "$HEARTBEAT_FILE.tmp" "$HEARTBEAT_FILE"
}

# "<state> <starttime in clock ticks>", fields 3 and 22 of /proc/<pid>/stat
proc_stat() {
  stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  set -- ${stat##*) }
  echo "$1 ${20}"
}

proc_ctxt() {
  awk '/ctxt_switches/ { s += $2 } END { print s + 0 }' "/proc/$1/status" 2>/dev/null
}

last_note() { cat "$STATE_DIR/$1.note" 2>/dev/null; }

# note <key> <verdict id> <message>: logs once per verdict, not on every pass
note() {
  [ "$(last_note "$1")" = "$2" ] && return 0
  echo "$2" > "$STATE_DIR/$1.note"
  log "pid=${1%%_*} $3"
}

if grep -q watchdog.sh /proc/1/cmdline 2>/dev/null; then
  log "FATAL: host processes are not visible, hostPID is required"
  exit 1
fi

wchan_ok=false
for i in 1 2 3 4 5; do
  for p in 1 2; do
    w=$(cat "/proc/$p/wchan" 2>/dev/null)
    [ -n "$w" ] && [ "$w" != "0" ] && wchan_ok=true
  done
  [ "$wchan_ok" = "true" ] && break
  sleep 1
done
if [ "$wchan_ok" != "true" ]; then
  log "FATAL: cannot read /proc/<pid>/wchan of host processes, SYS_PTRACE is required"
  exit 1
fi

mkdir -p "$STATE_DIR"
heartbeat
log "started min_age=${MIN_AGE_SECONDS}s confirm=${CONFIRM_SECONDS}s max_ctxt_delta=$MAX_CTXT_DELTA interval=${INTERVAL_SECONDS}s dry_run=$DRY_RUN"

while :; do
  now=$(date +%s)
  up=$(cut -d. -f1 /proc/uptime)
  seen=""

  for f in $(grep -lxE 'mount\.nfs4?' /proc/[0-9]*/comm 2>/dev/null); do
    pid=${f#/proc/}
    pid=${pid%/comm}
    st=$(proc_stat "$pid") || continue
    state=${st% *}
    start=${st#* }
    key="${pid}_${start}"
    seen="$seen $key"
    age=$(( up - start / 100 ))
    [ "$age" -ge "$MIN_AGE_SECONDS" ] || continue

    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    case "$cmd" in
      *" /var/lib/kubelet/"*) ;;
      *)
        note "$key" ignored "ignored, not a kubelet volume mount: $cmd"
        continue
        ;;
    esac

    if [ "$(last_note "$key")" = "killed" ]; then
      note "$key" unkillable "still present after SIGKILL, node reboot needed: $cmd"
      continue
    fi
    [ "$(last_note "$key")" = "unkillable" ] && continue

    wchan=$(cat "/proc/$pid/wchan" 2>/dev/null)
    case "$state:$wchan" in
      D:rpc_wait_bit_killable*) ;;
      D:nfs_wait_client_init_complete*)
        rm -f "$STATE_DIR/$key"
        note "$key" waiter "age=${age}s queued behind another mount, left alone: $cmd"
        continue
        ;;
      *)
        rm -f "$STATE_DIR/$key"
        note "$key" "unknown:$state:$wchan" "age=${age}s unrecognised state=$state wchan=$wchan, left alone: $cmd"
        continue
        ;;
    esac

    # blocked on an RPC reply: require next to no scheduling activity for
    # CONFIRM_SECONDS. A stuck task still wakes once per RPC timeout (timeo, 60s)
    # only to go back to sleep, a mount that makes progress switches far more often.
    ctxt=$(proc_ctxt "$pid")
    first=""
    prev=""
    [ -f "$STATE_DIR/$key" ] && read -r first prev < "$STATE_DIR/$key"
    if [ -z "$first" ]; then
      echo "$now $ctxt" > "$STATE_DIR/$key"
      note "$key" observing "age=${age}s waiting for an RPC reply, observing for ${CONFIRM_SECONDS}s: $cmd"
      continue
    fi
    [ $(( now - first )) -ge "$CONFIRM_SECONDS" ] || continue
    delta=$(( ctxt - prev ))
    echo "$now $ctxt" > "$STATE_DIR/$key"
    if [ "$delta" -gt "$MAX_CTXT_DELTA" ]; then
      log "pid=$pid is active ($delta context switches in $(( now - first ))s), observation restarted"
      continue
    fi

    # same process, still in the same state, right before the signal
    [ "$(proc_stat "$pid")" = "D $start" ] || continue
    case "$(cat "/proc/$pid/wchan" 2>/dev/null)" in rpc_wait_bit_killable*) ;; *) continue ;; esac

    if [ "$DRY_RUN" = "true" ]; then
      if kill -0 "$pid" 2>/dev/null; then perm=allowed; else perm=DENIED; fi
      note "$key" dry-run "DRY_RUN would kill (signal $perm): age=${age}s, $delta context switches in $(( now - first ))s: $cmd"
    elif kill -9 "$pid" 2>/dev/null; then
      echo killed > "$STATE_DIR/$key.note"
      log "pid=$pid KILLED stuck mount: age=${age}s, $delta context switches in $(( now - first ))s: $cmd"
    else
      log "pid=$pid kill FAILED: $cmd"
    fi
  done

  for s in "$STATE_DIR"/*; do
    [ -e "$s" ] || continue
    k=${s##*/}
    k=${k%.note}
    case " $seen " in
      *" $k "*) ;;
      *)
        [ "${s%.note}" != "$s" ] && log "pid=${k%%_*} gone"
        rm -f "$s"
        ;;
    esac
  done

  heartbeat
  sleep "$INTERVAL_SECONDS"
done
