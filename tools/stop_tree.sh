#!/usr/bin/env bash
# Stop a process and EVERY descendant (2026-11-20: killing a job's main process and
# its direct children left grandchild combosolvers running as orphans, eating the
# cores of the job that replaced it). Usage: tools/stop_tree.sh <pid> [signal]
root=$1; sig=${2:-TERM}
[ -n "$root" ] || { echo "usage: $0 <pid> [signal]" >&2; exit 1; }
tree() { local p=$1; echo "$p"; for c in $(pgrep -P "$p"); do tree "$c"; done; }
pids=$(tree "$root")
kill -STOP $pids 2>/dev/null        # freeze first, so nothing respawns mid-walk
kill -"$sig" $pids 2>/dev/null
kill -CONT $pids 2>/dev/null
sleep 3
left=$(for p in $pids; do kill -0 "$p" 2>/dev/null && echo "$p"; done)
[ -n "$left" ] && kill -KILL $left 2>/dev/null
echo "stopped $(echo $pids | wc -w) process(es) under $root"
