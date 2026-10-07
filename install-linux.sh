#!/bin/sh
# Linux installer for Frontier Usage. Run:   sh install-linux.sh
#
# Installs Python 3, Tk and secret-tool (for saving keys in the desktop keyring) with your
# distro's package manager (apt, dnf, yum, zypper, pacman or apk; asks for your sudo password),
# then adds "Frontier Usage" to the applications menu and the Desktop.
#
# DRY_RUN=1 sh install-linux.sh   prints what it would do without changing anything.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*" >&2; }
fail() { printf '\nInstall failed: %s\n' "$*" >&2; exit 1; }
run()  { if [ -n "${DRY_RUN:-}" ]; then echo "    + $*"; else "$@"; fi; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""
elif command -v sudo >/dev/null 2>&1; then SUDO="sudo"
else fail "run this as root or install sudo"; fi
as_root() { run $SUDO "$@"; }

good_python() {
  "$1" -c 'import sys, _tkinter; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

# Package names per package manager: <python+tk> then <secret-tool, optional>.
if   command -v apt-get >/dev/null 2>&1; then PM=apt
  BASE="python3 python3-tk ca-certificates"; EXTRA="libsecret-tools"
elif command -v dnf     >/dev/null 2>&1; then PM=dnf
  BASE="python3 python3-tkinter ca-certificates"; EXTRA="libsecret"
elif command -v yum     >/dev/null 2>&1; then PM=yum
  BASE="python3 python3-tkinter ca-certificates"; EXTRA="libsecret"
elif command -v zypper  >/dev/null 2>&1; then PM=zypper
  BASE="python3 python3-tk ca-certificates"; EXTRA="libsecret-tools"
elif command -v pacman  >/dev/null 2>&1; then PM=pacman
  BASE="python tk ca-certificates"; EXTRA="libsecret"
elif command -v apk     >/dev/null 2>&1; then PM=apk
  BASE="python3 python3-tkinter ca-certificates"; EXTRA="libsecret"
else
  fail "no supported package manager found. Install Python 3 with Tk (python3-tk) yourself, then run gui.py."
fi

pm_install() {
  case $PM in
    apt)    { [ -n "${APT_UPDATED:-}" ] || { as_root apt-get update -q && APT_UPDATED=1; }; } && as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$@" ;;
    dnf)    as_root dnf install -y -q "$@" ;;
    yum)    as_root yum install -y -q "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    pacman) as_root pacman -S --needed --noconfirm "$@" ;;
    apk)    as_root apk add "$@" ;;
  esac
}

say "Frontier Usage installer for Linux ($PM)"
say "Installing Python 3 and Tk"
# shellcheck disable=SC2086
pm_install $BASE || fail "package install failed (see messages above)"

say "Installing secret-tool (lets the app save keys in your desktop keyring)"
# shellcheck disable=SC2086
pm_install $EXTRA || warn "couldn't install secret-tool; keys will be saved in ~/.config/frontier-usage instead"

PY="$(command -v python3 || command -v python || true)"
[ -n "${DRY_RUN:-}" ] && PY="${PY:-/usr/bin/python3}"
if [ -z "${DRY_RUN:-}" ]; then
  [ -n "$PY" ] && good_python "$PY" || fail "Python 3.8+ with Tk still isn't working after install."
  echo "    Using $("$PY" -V 2>&1) at $PY"
fi

say "Setting up launchers"
run chmod +x "$HERE/Frontier Usage.sh" "$HERE/gui.py" "$HERE/fetch_usage.py"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
entry="[Desktop Entry]
Type=Application
Name=Frontier Usage
Comment=Token usage by user across AI providers
Exec=\"$PY\" \"$HERE/gui.py\"
Path=$HERE
Icon=utilities-system-monitor
Terminal=false
Categories=Office;Utility;"
desktop_dir="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP || echo "$HOME/Desktop")"
for f in "$apps/frontier-usage.desktop" "$desktop_dir/frontier-usage.desktop"; do
  [ "$f" = "$desktop_dir/frontier-usage.desktop" ] && [ ! -d "$desktop_dir" ] && continue
  if [ -n "${DRY_RUN:-}" ]; then echo "    + write $f"; continue; fi
  mkdir -p "$(dirname "$f")" && printf '%s\n' "$entry" > "$f" && chmod +x "$f"
  # GNOME won't launch Desktop icons until they're marked trusted.
  command -v gio >/dev/null 2>&1 && gio set "$f" metadata::trusted true 2>/dev/null || true
done

say "Done. Open \"Frontier Usage\" from your applications menu or Desktop,"
echo "    or run:  \"$HERE/Frontier Usage.sh\""
