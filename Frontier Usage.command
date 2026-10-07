#!/bin/bash
# macOS: double-click to open the Frontier Usage window.
cd "$(dirname "$0")" || exit 1
for py in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3 /usr/local/bin/python3 /opt/homebrew/bin/python3 /usr/bin/python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import _tkinter" 2>/dev/null; then
    "$py" gui.py
    exit $?
  fi
done
osascript -e 'display alert "Python is needed" message "Install Python 3 from python.org (it includes the window toolkit), then double-click this again."'
open "https://www.python.org/downloads/macos/"
