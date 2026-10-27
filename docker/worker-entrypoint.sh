#!/bin/sh
# Start a virtual display for headful Chrome, then run the worker as the main process.
set -e

# A restarted container keeps /tmp, so clear any display state left by the previous run.
rm -f /tmp/.X11-unix/X99 /tmp/.X99-lock

Xvfb :99 -screen 0 1280x960x24 -nolisten tcp &
XVFB_PID=$!
export DISPLAY=:99

# Wait for Xvfb to create its socket.
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do
  kill -0 "$XVFB_PID" 2>/dev/null || break
  sleep 0.1
  i=$((i + 1))
done

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo "worker-entrypoint: Xvfb exited during startup" >&2
  exit 1
fi
if [ ! -e /tmp/.X11-unix/X99 ]; then
  echo "worker-entrypoint: Xvfb did not create /tmp/.X11-unix/X99 within 5s" >&2
  exit 1
fi

exec python -m server.worker
