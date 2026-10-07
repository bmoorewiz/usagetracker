#!/bin/bash
# macOS installer for Frontier Usage: double-click (or run in Terminal).
#
# 1. Finds a Python 3.8+ that has Tk, or installs one:
#      - with Homebrew if it's already installed (brew install python python-tk), otherwise
#      - the official python.org package (asks for your Mac password).
# 2. Installs certifi so HTTPS works with python.org's Python.
# 3. Clears the "downloaded from the internet" flag on this folder and puts a
#    "Frontier Usage" launcher on your Desktop.
#
# DRY_RUN=1 ./install-mac.command   prints what it would do without changing anything.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
FALLBACK_VERSION="3.12.10"   # used only if python.org's download page can't be read
TMP="${TMPDIR:-/tmp}"

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*" >&2; }
run()  { if [ -n "${DRY_RUN:-}" ]; then echo "    + $*"; else "$@"; fi; }
fail() {
  printf '\nInstall failed: %s\n' "$*" >&2
  [ -z "${DRY_RUN:-}" ] && osascript -e "display alert \"Frontier Usage install failed\" message \"$*\"" >/dev/null 2>&1
  exit 1
}

# A usable Python: 3.8+, imports tkinter. /usr/bin/python3 is skipped on purpose: it can pop up
# an Xcode tools dialog and its Tk 8.5 is deprecated by Apple.
good_python() {
  "$1" -c 'import sys, _tkinter; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

find_python() {
  local c
  for c in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ "$c" != /usr/bin/python3 ] && [ -x "$c" ] && good_python "$c" && { echo "$c"; return 0; }
  done
  return 1
}

latest_python_org() {
  # Newest stable "python-X.Y.Z-macos11.pkg" on the download page (pre-releases don't match).
  curl -fsSL --max-time 20 https://www.python.org/downloads/macos/ 2>/dev/null \
    | grep -oE 'python-[0-9]+\.[0-9]+\.[0-9]+-macos11\.pkg' \
    | sed -E 's/python-([0-9.]+)-macos11\.pkg/\1/' \
    | sort -u -t. -k1,1n -k2,2n -k3,3n | tail -1
}

install_with_brew() {
  say "Installing Python and Tk with Homebrew"
  run brew install python python-tk || return 1
}

install_python_org() {
  local ver pkg
  ver="$(latest_python_org)"
  [ -n "$ver" ] || { warn "couldn't read python.org; using $FALLBACK_VERSION"; ver="$FALLBACK_VERSION"; }
  pkg="$TMP/python-$ver-macos11.pkg"
  say "Downloading Python $ver from python.org"
  run curl -fL --progress-bar -o "$pkg" "https://www.python.org/ftp/python/$ver/python-$ver-macos11.pkg" \
    || return 1
  say "Installing Python $ver (enter your Mac login password if asked)"
  run sudo installer -pkg "$pkg" -target / || return 1
  run rm -f "$pkg"
  # python.org's Python needs this once or every HTTPS call fails with CERTIFICATE_VERIFY_FAILED.
  local certs="/Applications/Python ${ver%.*}/Install Certificates.command"
  if [ -n "${DRY_RUN:-}" ] || [ -x "$certs" ]; then run "$certs"; fi
}

say "Frontier Usage installer for macOS"
PY="$(find_python)"
if [ -n "$PY" ]; then
  echo "    Found $("$PY" -V 2>&1) with Tk at $PY"
else
  if command -v brew >/dev/null 2>&1; then
    install_with_brew || warn "Homebrew install failed; trying python.org instead"
  fi
  [ -n "${DRY_RUN:-}" ] || PY="$(find_python)"
  if [ -z "$PY" ]; then
    install_python_org || fail "Couldn't download or install Python. Check your internet connection and try again."
    [ -n "${DRY_RUN:-}" ] && PY=/Library/Frameworks/Python.framework/Versions/Current/bin/python3 || PY="$(find_python)"
  fi
  [ -n "$PY" ] || fail "Python was installed but Tk isn't working. Install Python from python.org manually."
fi

# Only python.org builds lack a CA bundle; Homebrew's Python uses OpenSSL's and refuses pip --user.
case "$PY" in
  /Library/Frameworks/*)
    if [ -n "${DRY_RUN:-}" ] || ! "$PY" -c "import certifi" 2>/dev/null; then
      say "Installing Python packages (certifi)"
      run "$PY" -m pip install --user --upgrade --disable-pip-version-check --quiet certifi 2>/dev/null \
        || warn "couldn't install certifi; HTTPS may fail until you run 'Install Certificates' in /Applications/Python 3.x"
    fi ;;
esac

say "Checking HTTPS"
if [ -z "${DRY_RUN:-}" ]; then
  if ! "$PY" - <<'EOF'
import sys, urllib.error, urllib.request
sys.path.insert(0, ".")
try:
    import certifi, ssl
    ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    ctx = None
try:
    urllib.request.urlopen("https://api.anthropic.com/", timeout=15, context=ctx)
except urllib.error.HTTPError:
    pass                                 # any HTTP status means TLS worked
except urllib.error.URLError as e:
    if "CERTIFICATE" in str(e.reason).upper():
        sys.exit("certificate check failed")
    print(f"    (couldn't reach api.anthropic.com: {e.reason}; skipping)")
EOF
  then warn "HTTPS certificate check failed; reports may not download until certifi is installed"; fi
fi

say "Setting up launchers"
run xattr -dr com.apple.quarantine "$HERE" 2>/dev/null || true
run chmod +x "$HERE/Frontier Usage.command" "$HERE/gui.py" "$HERE/fetch_usage.py"
shortcut="$HOME/Desktop/Frontier Usage.command"
if [ -n "${DRY_RUN:-}" ]; then
  echo "    + write $shortcut"
else
  printf '#!/bin/bash\n# Created by install-mac.command\ncd "%s" || exit 1\nexec "%s" gui.py\n' "$HERE" "$PY" > "$shortcut"
  chmod +x "$shortcut"
fi

say "Done. Double-click \"Frontier Usage\" on your Desktop to start."
if [ -z "${DRY_RUN:-}" ] && [ -t 0 ]; then
  read -r -p "Open it now? [Y/n] " ans
  case "${ans:-y}" in [Yy]*) (cd "$HERE" && nohup "$PY" gui.py >/dev/null 2>&1 &) ;; esac
fi
