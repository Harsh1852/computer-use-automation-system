#!/usr/bin/env bash
# Bring up the virtual display the headed browser renders into, plus the two
# VNC bridges an operator uses to take over the session.
#
# The browser runs headed on purpose. A human operator has to be able to take
# over the *same* session later, and you cannot hand a headless browser to a
# person.
#
# Two x11vnc servers on one display, and this is the important part:
#
#   :5900  started with -viewonly  -> noVNC on 6080   (monitor)
#   :5901  started without it      -> noVNC on 6081   (interactive)
#
# View-only is a property of the server that owns the socket, not a
# `view_only=true` parameter in a noVNC URL that anybody watching could delete
# from the address bar. The console embeds 6080 and only swaps to 6081 once
# the lease has actually transferred, so the UI follows the authority rather
# than being it.
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1280x900x24}"
ENABLE_VNC="${ENABLE_VNC:-1}"
NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"

start_xvfb() {
  if xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1; then
    return
  fi
  # A stopped container keeps its filesystem, so `docker compose up -d` after
  # a Docker restart re-enters a container still holding the lock file from
  # its previous life. Xvfb then refuses to start, the entrypoint exits, and
  # every later `exec` fails with "service cua is not running" - a dead
  # runtime whose cause is buried in the container log.
  #
  # xdpyinfo has just told us nothing is listening on this display, so any
  # lock still present belongs to a server that no longer exists.
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
  # Xvfb prints ~40 lines of "could not resolve keysym" on every start. They
  # are harmless, they are not ours, and they land on the console in front of
  # whatever the command was actually asked to print. Kept in a file so a
  # genuine failure is still diagnosable.
  Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp \
    >/tmp/xvfb.log 2>&1 &
  for _ in $(seq 1 50); do
    xdpyinfo -display ":${DISPLAY_NUM}" >/dev/null 2>&1 && return
    sleep 0.1
  done
  echo "Xvfb failed to start on :${DISPLAY_NUM}" >&2
  tail -n 20 /tmp/xvfb.log >&2 || true
  exit 1
}

start_vnc() {
  # -forever so the bridge survives an operator closing the tab; -shared so
  # the monitor view and the interactive view can both be attached at once,
  # which is what makes "watch, then take over" a continuous experience.
  x11vnc -display ":${DISPLAY_NUM}" -viewonly -rfbport 5900 \
         -forever -shared -nopw -quiet -bg >/dev/null 2>&1
  x11vnc -display ":${DISPLAY_NUM}" -rfbport 5901 \
         -forever -shared -nopw -quiet -bg >/dev/null 2>&1

  websockify --web="${NOVNC_DIR}" 6080 localhost:5900 >/dev/null 2>&1 &
  websockify --web="${NOVNC_DIR}" 6081 localhost:5901 >/dev/null 2>&1 &
}

start_xvfb
if [ "${ENABLE_VNC}" = "1" ]; then
  start_vnc
fi

export DISPLAY=":${DISPLAY_NUM}"
exec "$@"
