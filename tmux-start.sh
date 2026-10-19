#!/bin/bash
# Start the web app + job worker in tmux session 'duelroom' (idempotent).
# Mirrors the launch sequence in web/PLAN.md.
tmux has-session -t duelroom 2>/dev/null && exit 0

cd /home/deezer/duelroom || exit 1

# The password hash and anything else secret live here, mode 600, outside
# git. Without it gunicorn refuses to start rather than serve an install
# nobody can log into.
if [ ! -f data/web.env ]; then
    echo "duelroom: data/web.env missing — run:" >&2
    echo "  venv/bin/python web/manage.py set-password" >&2
    exit 1
fi

tmux new-session -d -s duelroom -n web    -c /home/deezer/duelroom
tmux new-window  -t duelroom   -n worker  -c /home/deezer/duelroom

# Two workers is plenty: the solver takes 6 of this box's 8 cores and the
# web side only ever reads files and SQLite. The login limiter lives in
# SQLite precisely so more than one worker is safe.
tmux send-keys -t duelroom:web "source venv/bin/activate && set -a && \
  . ./data/web.env && set +a && DUELROOM_TLS=1 \
  gunicorn -w 2 --worker-class gthread --threads 8 \
  --certfile certs/fullchain.pem --keyfile certs/key.pem \
  -b 0.0.0.0:8446 web.wsgi:app" C-m

# Exactly one. One process is what makes 'one solver job at a time'
# structural rather than a convention.
tmux send-keys -t duelroom:worker "source venv/bin/activate && \
  python web/worker.py" C-m
exit 0
