#!/bin/sh
# Linux: run (or double-click and choose "Run") to open the Frontier Usage window.
cd "$(dirname "$0")" || exit 1
if python3 -c "import _tkinter" 2>/dev/null; then
  exec python3 gui.py
fi
msg="Python 3 with Tk is needed. Install it with: sudo apt install python3-tk   (Fedora: sudo dnf install python3-tkinter)"
command -v zenity >/dev/null && zenity --error --text="$msg" || echo "$msg"
