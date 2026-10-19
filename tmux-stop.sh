#!/bin/bash
# Killing the tmux session only SIGHUPs gunicorn, which it treats as a graceful
# *reload* -- the master orphans itself and keeps holding :8446. So kill the
# gunicorn processes explicitly after dropping the session. (Learned on
# stockroom; the same footgun, the same fix.)

# The worker FIRST, and politely. It handles SIGTERM by finishing the job it
# holds and then stopping; dropping the tmux session instead SIGHUPs it dead
# mid-solve, and the next start marks that job "worker restarted while this
# job was running". Two of the user's runs were lost that way before this
# existed. Grace is bounded because an optimize run lasts hours -- past it
# the job is lost as before, which is why `jobs` should be checked first.
GRACE="${DUELROOM_STOP_GRACE:-180}"
worker=$(pgrep -u deezer -f 'python web/worker\.py$' | head -1)
if [ -n "$worker" ]; then
    kill "$worker" 2>/dev/null
    for _ in $(seq 1 "$GRACE"); do
        kill -0 "$worker" 2>/dev/null || break
        sleep 1
    done
fi

tmux kill-session -t duelroom 2>/dev/null

# `pgrep -f` matches whole command lines, so ANY process that merely *mentions*
# these strings is a candidate -- including the shell that just ran a command
# containing them. That has taken out an unrelated shell on this box before.
# Never kill ourselves, our parent, or anything in our own process group.
parent=$PPID
mypgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
pids_for() {
    local p pg
    pgrep -u deezer -f "$1" | while read -r p; do
        [ "$p" = "$parent" ] && continue
        pg=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')
        [ -n "$pg" ] && [ "$pg" = "$mypgid" ] && continue
        printf '%s\n' "$p"
    done
}

GUNI='gunicorn .*web\.wsgi:app'
gpids=$(pids_for "$GUNI")
if [ -n "$gpids" ]; then
    kill $gpids 2>/dev/null
    for _ in $(seq 1 15); do
        [ -z "$(pids_for "$GUNI")" ] && break
        sleep 1
    done
    remaining=$(pids_for "$GUNI")
    [ -n "$remaining" ] && kill -9 $remaining 2>/dev/null
fi

# A solver job can be mid-sweep. SIGTERM makes the worker finish the job it
# holds and then stop; killing it harder leaves a partial run on disk that
# reads as complete, which is the one failure the job queue exists to prevent.
wpids=$(pids_for 'python web/worker\.py$')
if [ -n "$wpids" ]; then
    kill $wpids 2>/dev/null
fi
exit 0
