#!/usr/bin/env bash
# Install UrStack (user or system).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Double-click from a file manager has no terminal — reopen in one so output is visible.
_stackup_open_in_terminal() {
  local script="$ROOT/install.sh"
  local title="UrStack Installer"
  export STACKUP_INSTALL_IN_TERM=1
  if command -v konsole &>/dev/null; then
    exec konsole --title "$title" -e bash "$script" "$@"
  elif command -v ptyxis &>/dev/null; then
    exec ptyxis --title "$title" -- bash "$script" "$@"
  elif command -v gnome-terminal &>/dev/null; then
    exec gnome-terminal --title="$title" -- bash "$script" "$@"
  elif command -v kgx &>/dev/null; then
    exec kgx -e bash "$script" "$@"
  elif command -v xterm &>/dev/null; then
    exec xterm -T "$title" -e bash "$script" "$@"
  elif command -v xdg-terminal-exec &>/dev/null; then
    exec xdg-terminal-exec bash "$script" "$@"
  fi
  # Last resort: notify + run anyway (output may be invisible)
  if command -v notify-send &>/dev/null; then
    notify-send "UrStack" "Open a terminal and run: $script" || true
  fi
  return 1
}

# Keep throwaway terminals open (Desktop Terminal=true, or terminals we opened).
_stackup_hold_terminal() {
  local ec=$?
  # Avoid pausing when we re-exec'd into another terminal (this process is exiting).
  [[ -n "${STACKUP_INSTALL_HOLD:-}" ]] || return "$ec"
  if [[ -t 0 ]] || [[ -n "${STACKUP_INSTALL_IN_TERM:-}" ]]; then
    echo
    if [[ $ec -ne 0 ]]; then
      echo "Install failed (exit $ec)." >&2
    fi
    read -r -p "Press Enter to close…" _ || true
  fi
  return "$ec"
}

if [[ -z "${STACKUP_INSTALL_IN_TERM:-}" ]] && [[ ! -t 1 ]]; then
  _stackup_open_in_terminal "$@" || true
fi

# Hold the window open for GUI/.desktop launches (and any interactive TTY run).
STACKUP_INSTALL_HOLD=1
trap '_stackup_hold_terminal' EXIT

MODE="user"
PROFILE="auto"
DO_UNINSTALL=0

usage() {
  cat <<EOF
Usage: ./install.sh [--user|--system] [--profile auto|default|developer] [--uninstall]

  UrStack — update your whole Fedora stack and install popular apps.

  --user       Install for current user (default) under ~/.local
  --system     Install to /usr/local (requires sudo; includes PolicyKit)
  --profile    How to seed config.conf when missing:
                 auto       Scan this workstation (default)
                 default    Core sources only
                 developer  All plugins enabled
  --uninstall  Remove installed files for the chosen mode
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE=user ;;
    --system) MODE=system ;;
    --profile) PROFILE="${2:-auto}"; shift ;;
    --uninstall) DO_UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

if [[ "$MODE" == "user" ]]; then
  APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/stackup"
  BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
  APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/96x96/apps"
  POLKIT_DIR=""
  LIBEXEC_DIR=""
else
  APP_HOME="/usr/local/share/stackup"
  BIN_DIR="/usr/local/bin"
  APP_DIR="/usr/local/share/applications"
  ICON_DIR="/usr/local/share/icons/hicolor/96x96/apps"
  POLKIT_DIR="/usr/share/polkit-1/actions"
  LIBEXEC_DIR="/usr/local/libexec"
fi

CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/stackup"
CFG_FILE="$CFG_DIR/config.conf"

run_priv() {
  if [[ "$MODE" == "system" && "$(id -u)" -ne 0 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

uninstall() {
  echo "Removing $APP_HOME ..."
  run_priv rm -rf "$APP_HOME"
  # Also remove legacy install path
  run_priv rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/fedora-workstation-updater" 2>/dev/null || true
  run_priv rm -f "$BIN_DIR/stackup" "$BIN_DIR/fedora-updates"
  run_priv rm -f "$APP_DIR/stackup.desktop" "$APP_DIR/com.local.stackup.desktop" "$APP_DIR/fedora-updates.desktop"
  run_priv rm -f "$ICON_DIR/stackup.png" "$ICON_DIR/fedora-updates.png"
  if [[ -n "$LIBEXEC_DIR" ]]; then
    run_priv rm -f "$LIBEXEC_DIR/stackup-priv" "$LIBEXEC_DIR/fedora-updates-priv"
  fi
  if [[ -n "$POLKIT_DIR" ]]; then
    run_priv rm -f "$POLKIT_DIR/com.local.stackup.policy" \
      "$POLKIT_DIR/com.local.fedoraworkstationupdater.policy"
  fi
  echo "Uninstalled. Config kept at: $CFG_FILE"
  echo "Logs kept under: ${XDG_STATE_HOME:-$HOME/.local/state}/stackup"
}

if [[ $DO_UNINSTALL -eq 1 ]]; then
  uninstall
  exit 0
fi

echo "Installing UrStack ($MODE, profile=$PROFILE)..."
run_priv mkdir -p "$APP_HOME" "$BIN_DIR" "$APP_DIR" "$ICON_DIR"
if [[ -n "$LIBEXEC_DIR" ]]; then
  run_priv mkdir -p "$LIBEXEC_DIR"
fi

run_priv rsync -a --delete \
  --exclude '.git' \
  --exclude '*.pyc' \
  --exclude '__pycache__' \
  "$ROOT/" "$APP_HOME/"

run_priv chmod +x "$APP_HOME/bin/stackup" "$APP_HOME/lib/core/priv.sh" "$APP_HOME/lib/core/ui.py"
[[ -f "$APP_HOME/bin/fedora-updates" ]] && run_priv chmod +x "$APP_HOME/bin/fedora-updates"

# PATH wrappers
cat > /tmp/stackup-wrapper.$$ <<EOF
#!/usr/bin/env bash
exec "$APP_HOME/bin/stackup" "\$@"
EOF
run_priv mv /tmp/stackup-wrapper.$$ "$BIN_DIR/stackup"
run_priv chmod +x "$BIN_DIR/stackup"
# Compatibility alias
run_priv ln -sfn stackup "$BIN_DIR/fedora-updates"
run_priv ln -sfn stackup "$BIN_DIR/urstack"

# Desktop + icons
run_priv install -m 644 "$ROOT/data/stackup.desktop" "$APP_DIR/stackup.desktop"
# Wayland/Plasma match windows by GApplication id → desktop basename
run_priv install -m 644 "$ROOT/data/com.local.stackup.desktop" "$APP_DIR/com.local.stackup.desktop"
if [[ "$MODE" == "user" ]]; then
  sed -i "s|^Exec=.*|Exec=$BIN_DIR/stackup|" "$APP_DIR/stackup.desktop"
  sed -i "s|^Exec=.*|Exec=$BIN_DIR/stackup|" "$APP_DIR/com.local.stackup.desktop"
fi
# Remove old desktop entry name
run_priv rm -f "$APP_DIR/fedora-updates.desktop"

if [[ "$MODE" == "user" ]]; then
  HICOLOR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
else
  HICOLOR="/usr/local/share/icons/hicolor"
fi
if [[ -d "$ROOT/data/icons/hicolor" ]]; then
  while IFS= read -r -d '' iconfile; do
    rel="${iconfile#"$ROOT/data/icons/hicolor/"}"
    # prefer stackup.png names; also install fedora-updates.png copies as stackup if needed
    base=$(basename "$rel")
    dir=$(dirname "$rel")
    run_priv mkdir -p "$HICOLOR/$dir"
    if [[ "$base" == "stackup.png" || "$base" == "fedora-updates.png" ]]; then
      run_priv install -m 644 "$iconfile" "$HICOLOR/$dir/stackup.png"
    fi
  done < <(find "$ROOT/data/icons/hicolor" -type f \( -name 'stackup.png' -o -name 'fedora-updates.png' \) -print0)
fi
run_priv mkdir -p "$ICON_DIR"
run_priv install -m 644 "$ROOT/data/icons/hicolor/96x96/apps/stackup.png" \
  "$ICON_DIR/stackup.png" 2>/dev/null \
  || run_priv install -m 644 "$ROOT/data/icons/stackup.png" "$ICON_DIR/stackup.png"

if [[ -n "$LIBEXEC_DIR" ]]; then
  run_priv install -m 755 "$ROOT/lib/core/priv.sh" "$LIBEXEC_DIR/stackup-priv"
fi
if [[ -n "$POLKIT_DIR" && -f "$ROOT/data/polkit/com.local.stackup.policy" ]]; then
  run_priv install -m 644 "$ROOT/data/polkit/com.local.stackup.policy" \
    "$POLKIT_DIR/com.local.stackup.policy"
fi

# Migrate legacy config if needed
LEGACY_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/fedora-workstation-updater/config.conf"
mkdir -p "$CFG_DIR"
if [[ ! -f "$CFG_FILE" && -f "$LEGACY_CFG" ]]; then
  cp "$LEGACY_CFG" "$CFG_FILE"
  echo "Migrated config from fedora-workstation-updater → stackup"
fi

if [[ ! -f "$CFG_FILE" ]]; then
  if [[ "$PROFILE" == "auto" ]]; then
    echo "Scanning workstation to build config..."
    FEDORA_UPDATES_ROOT="$APP_HOME" STACKUP_ROOT="$APP_HOME" \
      bash "$APP_HOME/bin/stackup" --detect --write-config
  else
    src="$ROOT/config/${PROFILE}.conf"
    [[ -f "$src" ]] || src="$ROOT/config/default.conf"
    cp "$src" "$CFG_FILE"
    echo "Created config: $CFG_FILE (profile=$PROFILE)"
  fi
else
  echo "Keeping existing config: $CFG_FILE"
  echo "  Re-scan anytime: stackup --detect --write-config"
fi

update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HICOLOR" 2>/dev/null || true

echo
echo "Installed UrStack."
echo "  Launch:  urstack   (or stackup)"
echo "  Config:  $CFG_FILE"
echo "  App:     $APP_HOME"
echo
echo "Tip: stackup --detect --write-config"
echo "     Open Apps in the UI to browse popular software by category."
# Terminal hold (Press Enter) is handled by EXIT trap.
