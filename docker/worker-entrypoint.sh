#!/bin/sh
# Start a virtual display for headful Chrome, then run the worker as the main process.
set -e

Xvfb :99 -screen 0 1280x960x24 &
export DISPLAY=:99

# Give Xvfb a moment to create its socket.
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do
  sleep 0.1
  i=$((i + 1))
done

exec python -m server.worker
