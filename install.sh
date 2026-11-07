#!/usr/bin/env bash
# Sweep Lyrics installer: system deps + pipx install.
# Usage: ./install.sh [--no-optional]
set -euo pipefail

REPO="git+https://github.com/Nirune/sweep-lyrics"
OPTIONAL=1
[[ "${1:-}" == "--no-optional" ]] && OPTIONAL=0

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

# --- detect distro family ---
. /etc/os-release
ids=" ${ID:-} ${ID_LIKE:-} "
case "$ids" in
  *" arch "*)                     FAMILY=arch ;;
  *" debian "*|*" ubuntu "*)      FAMILY=debian ;;
  *" fedora "*|*" rhel "*)        FAMILY=fedora ;;
  *)                              FAMILY=unknown ;;
esac
say "Detected: ${PRETTY_NAME:-unknown} (family: $FAMILY)"

# --- package sets ---
case "$FAMILY" in
  arch)
    REQUIRED=(python-pipx kitty playerctl)
    OPTIONAL_PKGS=(libpulse libnotify)
    $SUDO pacman -S --needed --noconfirm "${REQUIRED[@]}"
    (( OPTIONAL )) && $SUDO pacman -S --needed --noconfirm "${OPTIONAL_PKGS[@]}"
    if (( OPTIONAL )) && ! have songrec; then
      if pacman -Si songrec >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm songrec
      elif have paru; then paru -S --needed songrec
      elif have yay;  then yay  -S --needed songrec
      else warn "songrec not found: install it from the AUR for Shazam mode (key 's')."
      fi
    fi
    ;;
  debian)
    REQUIRED=(pipx kitty playerctl)
    OPTIONAL_PKGS=(pulseaudio-utils libnotify-bin)
    $SUDO apt-get update
    $SUDO apt-get install -y "${REQUIRED[@]}"
    (( OPTIONAL )) && $SUDO apt-get install -y "${OPTIONAL_PKGS[@]}"
    if (( OPTIONAL )) && ! have songrec; then
      if apt-cache show songrec >/dev/null 2>&1; then
        $SUDO apt-get install -y songrec
      else
        warn "songrec not in your apt repos: Shazam mode (key 's') will be unavailable."
      fi
    fi
    ;;
  fedora)
    REQUIRED=(pipx kitty playerctl)
    OPTIONAL_PKGS=(pulseaudio-utils libnotify)
    $SUDO dnf install -y "${REQUIRED[@]}"
    (( OPTIONAL )) && $SUDO dnf install -y "${OPTIONAL_PKGS[@]}"
    (( OPTIONAL )) && ! have songrec && \
      warn "songrec not packaged in default repos: Shazam mode (key 's') needs a manual install."
    ;;
  *)
    warn "Unknown distro. Install manually: pipx, kitty, playerctl (+ optional: songrec, parec/pactl, notify-send)."
    have pipx || { warn "pipx is required, aborting."; exit 1; }
    ;;
esac

# --- the app itself ---
say "Installing sweep-lyrics via pipx"
pipx ensurepath >/dev/null 2>&1 || true
export PATH="$HOME/.local/bin:$PATH"
pipx install --force "$REPO"

# --- verify ---
if have sweeplyrics; then
  say "Done. Run: sweeplyrics   (open a new shell first if the command isn't found)"
else
  warn "Installed, but ~/.local/bin isn't on PATH yet. Open a new shell and run: sweeplyrics"
fi