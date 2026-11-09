#!/bin/sh
# Double-click to start the X2D Mixed Nozzle window (macOS).
# Uses the first Python 3.9+ whose Tkinter can open a window.
cd "$(dirname "$0")" || exit 1
for py in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
          /usr/local/bin/python3 /usr/local/bin/python3.* \
          /opt/homebrew/bin/python3 /opt/homebrew/bin/python3.* \
          python3 /usr/bin/python3; do
  case "$py" in *-config|*\*) continue ;; esac
  command -v "$py" >/dev/null 2>&1 || continue
  if "$py" -c "import sys, tkinter; sys.exit(1) if sys.version_info < (3, 9) else tkinter.Tk().destroy()" \
       >/dev/null 2>&1; then
    "$py" -I x2d_mixed_gui.py "$@" &
    exit 0
  fi
done
osascript -e 'display alert "X2D Mixed Nozzle" message "No Python 3.9 or newer with a working Tkinter was found. Install Python from python.org and try again."'
exit 1
