#!/usr/bin/env bash
# Bring up the virtual display the headed browser renders into, then hand off.
#
# The browser runs headed on purpose. A human operator has to be able to take
# over the *same* session later, and you cannot hand a headless browser to a
# person. Xvfb is what makes "headed" possible without a physical screen, and
# it is also what the VNC bridges will attach to.
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x900x24}"

if ! xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1; then
  Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &
  for _ in $(seq 1 50); do
    xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && break
    sleep 0.1
  done
fi

export DISPLAY=":${DISPLAY_NUM}"
exec "$@"
