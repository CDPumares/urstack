# Shared constants/helpers for UrStack (sourced).
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Paths / timeouts
# ---------------------------------------------------------------------------
# common.sh lives at <root>/lib/core/common.sh
: "${URSTACK_ROOT:=${STACKUP_ROOT:-${FEDORA_UPDATES_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}}}"
: "${STACKUP_ROOT:=$URSTACK_ROOT}"
: "${FEDORA_UPDATES_ROOT:=$URSTACK_ROOT}"
: "${FEDORA_UPDATES_LIB:=$URSTACK_ROOT/lib/core}"
: "${FEDORA_UPDATES_PLUGINS:=$URSTACK_ROOT/lib/plugins}"

# Prefer ~/.config/urstack; migrate fedora-workstation-updater then stackup once
_cfg_home="${XDG_CONFIG_HOME:-$HOME/.config}"
_new_cfg="$_cfg_home/urstack"
if [[ ! -d "$_new_cfg" ]]; then
  for _legacy_cfg in "$_cfg_home/stackup" "$_cfg_home/fedora-workstation-updater"; do
    if [[ -d "$_legacy_cfg" ]]; then
      mkdir -p "$_new_cfg" 2>/dev/null || true
      if [[ -d "$_new_cfg" ]]; then
        cp -a "$_legacy_cfg/." "$_new_cfg/" 2>/dev/null || true
      fi
      break
    fi
  done
fi
: "${FEDORA_UPDATES_CONFIG_DIR:=$_new_cfg}"
: "${FEDORA_UPDATES_USER_CONFIG:=$FEDORA_UPDATES_CONFIG_DIR/config.conf}"

_state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
_new_log="$_state_home/urstack"
if [[ ! -d "$_new_log" ]]; then
  for _legacy_log in "$_state_home/stackup" "$_state_home/fedora-workstation-updater"; do
    if [[ -d "$_legacy_log" ]]; then
      mkdir -p "$_new_log" 2>/dev/null || true
      if [[ -d "$_new_log" ]]; then
        cp -a "$_legacy_log/." "$_new_log/" 2>/dev/null || true
      fi
      break
    fi
  done
fi
: "${LOG_DIR:=$_new_log}"
: "${LOG_FILE:=$LOG_DIR/urstack.log}"
: "${RUN_LOG_DIR:=}"
: "${LOCK_FILE:=${XDG_RUNTIME_DIR:-/tmp}/urstack-$UID.lock}"

APP_NAME="UrStack"
APP_ID="urstack"
APP_TAGLINE="Update your whole Fedora stack — and install popular apps"

TIMEOUT_DNF="${TIMEOUT_DNF:-120}"
CHECK_PARALLEL="${CHECK_PARALLEL:-4}"
TIMEOUT_SNAP="${TIMEOUT_SNAP:-15}"
TIMEOUT_FW="${TIMEOUT_FW:-20}"
TIMEOUT_FLATPAK="${TIMEOUT_FLATPAK:-45}"
TIMEOUT_NPM="${TIMEOUT_NPM:-15}"
TIMEOUT_PIP="${TIMEOUT_PIP:-20}"
TIMEOUT_PIPX="${TIMEOUT_PIPX:-15}"
TIMEOUT_RUST="${TIMEOUT_RUST:-15}"
TIMEOUT_CARGO="${TIMEOUT_CARGO:-20}"
TIMEOUT_NODE="${TIMEOUT_NODE:-15}"
TIMEOUT_CURSOR="${TIMEOUT_CURSOR:-15}"
TIMEOUT_CLAUDE="${TIMEOUT_CLAUDE:-15}"
TIMEOUT_SUPABASE="${TIMEOUT_SUPABASE:-15}"
TIMEOUT_TOOLBOX="${TIMEOUT_TOOLBOX:-45}"
TIMEOUT_AKMODS="${TIMEOUT_AKMODS:-600}"

# Free space thresholds (MiB)
MIN_ROOT_MIB="${MIN_ROOT_MIB:-2048}"
MIN_BOOT_MIB="${MIN_BOOT_MIB:-256}"

readonly DNF_EXCLUDE_PKGS=(
  plasma-discover
  plasma-discover-notifier
  plasma-discover-packagekit
  plasma-discover-flatpak
  plasma-discover-snap
  plasma-discover-kns
  plasma-discover-offline-updates
  plasma-discover-libs
  plasma-discover-rpm-ostree
)

# All known update sources (filtered by config into SECTION_KEYS)
readonly ALL_SECTION_KEYS=(dnf snap fw flatpak toolbox npm npm_user pip pipx rust cargo node cursor claude supabase)

# Privileged sections (handled by one pkexec batch)
readonly PRIV_SECTIONS=(dnf snap fw pip_deps cursor)

# Populated by load_updater_config
SECTION_KEYS=()
KEEP_KERNELS="${KEEP_KERNELS:-3}"
EXCLUDE_DISCOVER=1
QUIET_GNOME_SOFTWARE=1
ENABLE_BACKUP=0
ENABLE_KERNEL_PRUNE=1

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
cfg_get() {
  # cfg_get KEY DEFAULT — reads from loaded assoc or default
  local key="$1" default="${2:-0}"
  local var="CFG_${key}"
  printf '%s' "${!var-$default}"
}

source_enabled() {
  local key="$1"
  case "$key" in
    dnf)      [[ "$(cfg_get enable_dnf 1)" == "1" ]] ;;
    snap)     [[ "$(cfg_get enable_snap 1)" == "1" ]] ;;
    fw)       [[ "$(cfg_get enable_fw 1)" == "1" ]] ;;
    flatpak)  [[ "$(cfg_get enable_flatpak 1)" == "1" ]] ;;
    toolbox)  [[ "$(cfg_get enable_toolbox 0)" == "1" ]] ;;
    npm)      [[ "$(cfg_get enable_npm 0)" == "1" ]] ;;
    npm_user) [[ "$(cfg_get enable_npm_user 0)" == "1" ]] ;;
    pip)      [[ "$(cfg_get enable_pip 0)" == "1" ]] ;;
    pipx)     [[ "$(cfg_get enable_pipx 0)" == "1" ]] ;;
    rust)     [[ "$(cfg_get enable_rust 0)" == "1" ]] ;;
    cargo)    [[ "$(cfg_get enable_cargo 0)" == "1" ]] ;;
    node)     [[ "$(cfg_get enable_node 0)" == "1" ]] ;;
    cursor)   [[ "$(cfg_get enable_cursor 0)" == "1" ]] ;;
    claude)   [[ "$(cfg_get enable_claude 0)" == "1" ]] ;;
    supabase) [[ "$(cfg_get enable_supabase 0)" == "1" ]] ;;
    *) return 1 ;;
  esac
}

_load_conf_file() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"
    printf -v "CFG_${key}" '%s' "$val"
  done < "$file"
}

load_updater_config() {
  local shipped="${FEDORA_UPDATES_ROOT}/config/default.conf"
  _load_conf_file "$shipped"
  _load_conf_file "$FEDORA_UPDATES_USER_CONFIG"

  KEEP_KERNELS="$(cfg_get keep_kernels 3)"
  EXCLUDE_DISCOVER="$(cfg_get exclude_discover 1)"
  QUIET_GNOME_SOFTWARE="$(cfg_get quiet_gnome_software 1)"
  ENABLE_BACKUP="$(cfg_get enable_backup 0)"
  ENABLE_KERNEL_PRUNE="$(cfg_get enable_kernel_prune 1)"
  export KEEP_KERNELS

  SECTION_KEYS=()
  local s
  for s in "${ALL_SECTION_KEYS[@]}"; do
    source_enabled "$s" && SECTION_KEYS+=("$s")
  done
}

ensure_user_config() {
  mkdir -p "$FEDORA_UPDATES_CONFIG_DIR" 2>/dev/null || true
  if [[ ! -f "$FEDORA_UPDATES_USER_CONFIG" ]]; then
    local src="${FEDORA_UPDATES_ROOT}/config/default.conf"
    [[ -f "$src" ]] && cp "$src" "$FEDORA_UPDATES_USER_CONFIG"
  fi
}

section_label() {
  case "$1" in
    dnf)      echo "DNF packages" ;;
    snap)     echo "Snap" ;;
    fw)       echo "Firmware" ;;
    flatpak)  echo "Flatpak" ;;
    toolbox)  echo "Toolbx / Distrobox" ;;
    npm)      echo "npm global (nvm)" ;;
    npm_user) echo "npm user (~/.local)" ;;
    pip)      echo "pip packages" ;;
    pip_deps) echo "pip build deps" ;;
    pipx)     echo "pipx" ;;
    rust)     echo "rustup" ;;
    cargo)    echo "Cargo binaries" ;;
    node)     echo "Node.js (nvm)" ;;
    cursor)   echo "Cursor AI" ;;
    claude)   echo "Claude Code" ;;
    supabase) echo "Supabase CLI" ;;
    *)        echo "$1" ;;
  esac
}

# True when section key $1 is in the selected set ($2).
# Selected is "all", keys joined by "|", or (legacy) section labels. Exact
# tokens only — "pipx" must not match "pip", "npm_user" must not match "npm".
section_is_selected() {
  local key="$1"
  local selected="${2:-}"
  local label part
  [[ -z "$selected" ]] && return 1
  [[ "$selected" == "all" ]] && return 0
  label=$(section_label "$key")
  selected="${selected//$'\n'/|}"
  local IFS='|'
  # shellcheck disable=SC2086
  for part in $selected; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ -z "$part" ]] && continue
    [[ "$part" == "$key" || "$part" == "$label" ]] && return 0
  done
  return 1
}

# Prefer user toolchains (nvm / cargo / pipx) over distro binaries in /usr/bin.
prepend_user_toolchain_path() {
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  local nvm_bin="" prefix=""
  if [[ -d "$nvm_dir/versions/node" ]]; then
    nvm_bin=$(find "$nvm_dir/versions/node" -mindepth 2 -maxdepth 2 -type d -name bin 2>/dev/null | sort -V | tail -1)
  fi
  [[ -n "$nvm_bin" && -d "$nvm_bin" ]] && prefix="${nvm_bin}:"
  [[ -d "$HOME/.cargo/bin" ]] && prefix="${prefix}${HOME}/.cargo/bin:"
  [[ -d "$HOME/.local/bin" ]] && prefix="${prefix}${HOME}/.local/bin:"
  [[ -n "$prefix" ]] && export PATH="${prefix}${PATH}"
}

priv_helper_path() {
  if [[ -x /usr/local/libexec/urstack-priv ]]; then
    printf '%s' /usr/local/libexec/urstack-priv
  elif [[ -x /usr/local/libexec/stackup-priv ]]; then
    printf '%s' /usr/local/libexec/stackup-priv
  else
    printf '%s' "${FEDORA_UPDATES_LIB}/priv.sh"
  fi
}

# Prepend #env KEY=VAL lines so priv.sh can read them without `pkexec env`
# (which would make PolicyKit match `env` instead of urstack-priv).
priv_jobs_inject_env() {
  local jobs_file="$1"
  local tmp
  tmp=$(mktemp)
  {
    echo "#env TIMEOUT_AKMODS=${TIMEOUT_AKMODS:-600}"
    echo "#env KEEP_KERNELS=${KEEP_KERNELS:-3}"
    echo "#env EXCLUDE_DISCOVER=${EXCLUDE_DISCOVER:-1}"
    echo "#env HEALTH_JOURNAL_VACUUM=${HEALTH_JOURNAL_VACUUM:-500M}"
    [[ -n "${FEDORA_UPDATES_AKMODS_CANCEL:-}" ]] && \
      echo "#env FEDORA_UPDATES_AKMODS_CANCEL=${FEDORA_UPDATES_AKMODS_CANCEL}"
    cat "$jobs_file"
  } > "$tmp"
  mv -f "$tmp" "$jobs_file"
}

pkexec_priv() {
  local jobs_file="$1"
  local helper
  helper=$(priv_helper_path)
  chmod +x "$helper" 2>/dev/null || true
  pkexec "$helper" "$jobs_file"
}

# Supabase publishes per-architecture tarballs. Shared so the update path and the
# restore path cannot drift onto different assets.
supabase_release_asset() {
  case "$(uname -m)" in
    aarch64|arm64) printf '%s' "supabase_linux_arm64.tar.gz" ;;
    *)             printf '%s' "supabase_linux_amd64.tar.gz" ;;
  esac
}

version_gt() {
  [[ -n "$1" && -n "$2" && "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n1)" == "$1" ]]
}

dnf_exclude_args() {
  local p
  if [[ "${EXCLUDE_DISCOVER:-1}" != "1" ]]; then
    return 0
  fi
  for p in "${DNF_EXCLUDE_PKGS[@]}"; do
    printf -- '--exclude=%s\n' "$p"
  done
}

zenity_escape() {
  sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

emit_lines() {
  zenity_escape | while IFS= read -r line; do echo "# $line"; done
}

# ── Incomplete-tree marker ───────────────────────────────────────────────────
# A backup or a restore point only means anything once every step has run. The
# marker goes in before the first step and comes out after the last, so a tree
# left by a cancel, a crash or a power cut still says it is partial. Restoring
# a partial tree would apply whichever steps happened to finish and skip the
# rest, with nothing to say which were which.
URSTACK_INCOMPLETE_NAME=".INCOMPLETE"

mark_tree_incomplete() {
  local dir="$1" what="${2:-operation}"
  {
    echo "This $what did not finish."
    echo "Started: $(date -Iseconds)"
    echo
    echo "It is missing an unknown number of steps, so UrStack refuses to"
    echo "restore from it. Delete this folder, or create a new one."
  } > "$dir/$URSTACK_INCOMPLETE_NAME" 2>/dev/null || true
}

mark_tree_complete() { rm -f "${1:?}/$URSTACK_INCOMPLETE_NAME" 2>/dev/null || true; }

tree_is_incomplete() { [[ -f "$1/$URSTACK_INCOMPLETE_NAME" ]]; }

notify() {
  local summary="$1" body="${2:-}" urgency="${3:-normal}"
  [[ "$(cfg_get notifications 1)" == "1" ]] || return 0
  command -v notify-send &>/dev/null || return 0
  local icon="${FEDORA_UPDATES_ROOT}/data/icons/urstack.png"
  [[ -f "$icon" ]] || icon="${FEDORA_UPDATES_ROOT}/data/icons/urstack.png"
  [[ -f "$icon" ]] || icon="${FEDORA_UPDATES_ROOT}/data/icons/fedora-updates.png"
  [[ -f "$icon" ]] || icon="urstack"
  [[ -f "$icon" ]] || icon="system-software-update"
  local script="${FEDORA_UPDATES_ROOT}/bin/urstack"
  [[ -x "$script" ]] || script="$(command -v urstack 2>/dev/null || command -v stackup 2>/dev/null || command -v fedora-updates 2>/dev/null || true)"

  # Actionable notification: Open launches the updater.
  # Must fully detach (--wait would otherwise keep the updater/timer hung).
  if notify-send --help 2>&1 | grep -q -- '--action'; then
    local helper
    helper=$(mktemp)
    cat > "$helper" <<EOF
#!/usr/bin/env bash
action=\$(notify-send --app-name=$(printf '%q' "$APP_NAME") --urgency=$(printf '%q' "$urgency") \\
  --icon=$(printf '%q' "$icon") \\
  --action=open=Open\\ updater \\
  --wait \\
  $(printf '%q' "$summary") $(printf '%q' "$body") 2>/dev/null || true)
rm -f $(printf '%q' "$helper")
if [[ "\$action" == "open" && -n $(printf '%q' "$script") && -x $(printf '%q' "$script") ]]; then
  nohup $(printf '%q' "$script") </dev/null &>/dev/null &
fi
EOF
    chmod +x "$helper"
    if command -v setsid &>/dev/null; then
      setsid -f "$helper" </dev/null &>/dev/null
    else
      nohup "$helper" </dev/null &>/dev/null &
      disown 2>/dev/null || true
    fi
  else
    notify-send --app-name="$APP_NAME" --urgency="$urgency" \
      --icon="$icon" "$summary" "$body" 2>/dev/null || true
  fi
}

has_section() { [[ -f "${check_dir:-}/$1" ]]; }
read_check() { cat "${check_dir:-}/$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# Run logging
# ---------------------------------------------------------------------------
init_run_log() {
  mkdir -p "$LOG_DIR/runs" 2>/dev/null || true
  RUN_LOG_DIR="$LOG_DIR/runs/$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "$RUN_LOG_DIR"
  echo "$RUN_LOG_DIR" > "$LOG_DIR/last-run-dir"
  {
    echo "run_started=$(date -Iseconds)"
    echo "kernel=$(uname -r)"
    echo "user=$USER"
  } > "$RUN_LOG_DIR/meta.txt"
}

section_log() {
  local name="$1"
  [[ -n "$RUN_LOG_DIR" ]] || { cat; return 0; }
  tee -a "$RUN_LOG_DIR/${name}.log"
}

append_summary_log() {
  local summary="$1"
  printf '[%s]\n%s\n' "$(date -Iseconds)" "$summary" >> "$LOG_FILE" 2>/dev/null || true
  if [[ -n "$RUN_LOG_DIR" ]]; then
    printf '%s\n' "$summary" > "$RUN_LOG_DIR/summary.txt"
  fi
}

# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------
acquire_update_lock() {
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
  local holder=""
  holder=$(cat "$LOCK_FILE.pid" 2>/dev/null || true)
  if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
    rm -f "$LOCK_FILE" "$LOCK_FILE.pid"
  fi
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    holder=$(cat "$LOCK_FILE.pid" 2>/dev/null || true)
    local msg="Fedora Updates is already running${holder:+ (pid $holder)}."
    if [[ "${AUTO_YES:-0}" -eq 1 ]] || [[ "${CHECK_ONLY:-0}" -eq 1 ]]; then
      echo "$msg" >&2
    else
      notify "Updates already running" "$msg" "critical"
      local _ui="$FEDORA_UPDATES_LIB/ui.py"
      if [[ -n "${DISPLAY:-}" && -f "$_ui" ]]; then
        python3 "$_ui" message --type error --title "$APP_NAME" --text "$msg" 2>/dev/null || true
      elif command -v zenity &>/dev/null; then
        zenity --error --title="$APP_NAME" --text="$msg" 2>/dev/null || true
      fi
    fi
    return 1
  fi
  echo $$ > "$LOCK_FILE.pid" 2>/dev/null || true
  return 0
}

# ---------------------------------------------------------------------------
# Competing updaters (pause GNOME Software for this run only — never mask)
# ---------------------------------------------------------------------------
_GNOME_SOFTWARE_QUIETED=0

disable_competing_updaters() {
  [[ "${QUIET_GNOME_SOFTWARE:-1}" == "1" ]] || return 0
  # Leave an already-masked unit alone (user choice).
  if systemctl --user is-enabled gnome-software.service 2>/dev/null | grep -qx masked; then
    return 0
  fi
  systemctl --user stop gnome-software.service 2>/dev/null || true
  pkill -f 'gnome-software --gapplication-service' 2>/dev/null || true
  _GNOME_SOFTWARE_QUIETED=1
}

restore_competing_updaters() {
  [[ "${_GNOME_SOFTWARE_QUIETED:-0}" == "1" ]] || return 0
  _GNOME_SOFTWARE_QUIETED=0
  # Do not restart it — gnome-software is a D-Bus service and comes back on demand.
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
disk_avail_mib() {
  local mount="$1"
  df -Pm "$mount" 2>/dev/null | awk 'NR==2{print $4}'
}

preflight_checks() {
  local -a problems=()
  local root_mib boot_mib

  root_mib=$(disk_avail_mib /)
  boot_mib=$(disk_avail_mib /boot)
  if [[ -n "$root_mib" && "$root_mib" -lt "$MIN_ROOT_MIB" ]]; then
    problems+=("Low space on /: ${root_mib} MiB free (need ≥ ${MIN_ROOT_MIB} MiB)")
  fi
  if [[ -n "$boot_mib" && "$boot_mib" -lt "$MIN_BOOT_MIB" ]]; then
    problems+=("Low space on /boot: ${boot_mib} MiB free (need ≥ ${MIN_BOOT_MIB} MiB) — kernel updates may fail")
  fi

  if command -v nmcli &>/dev/null; then
    nmcli -t -f STATE g 2>/dev/null | grep -qx 'connected' \
      || problems+=("Network does not look connected (nmcli)")
  elif ! ping -c1 -W2 1.1.1.1 &>/dev/null && ! ping -c1 -W2 8.8.8.8 &>/dev/null; then
    problems+=("No network reachability (ping failed)")
  fi

  if pgrep -x dnf5 &>/dev/null || pgrep -x dnf &>/dev/null || pgrep -f '/usr/bin/dnf ' &>/dev/null; then
    problems+=("Another DNF process is running — wait for it to finish")
  fi

  # Repo health (non-fatal advisories)
  local -a advisories=()
  if [[ -f /etc/yum.repos.d/google-chrome.repo ]] || rpm -q google-chrome-stable &>/dev/null; then
    [[ -f /etc/yum.repos.d/google-chrome.repo ]] \
      || advisories+=("google-chrome.repo missing (Chrome updates via DNF may not appear)")
  fi
  if source_enabled cursor; then
    [[ -f /etc/yum.repos.d/cursor.repo ]] \
      || advisories+=("cursor.repo missing (will be created on apply if needed)")
  fi

  if [[ ${#problems[@]} -gt 0 ]]; then
    printf '%s\n' "${problems[@]}" > "${check_dir}/preflight_errors"
  fi
  if [[ ${#advisories[@]} -gt 0 ]]; then
    printf '%s\n' "${advisories[@]}" > "${check_dir}/preflight_advisories"
  fi

  [[ ${#problems[@]} -eq 0 ]]
}

latest_installed_kernel() {
  rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort -V | tail -1
}

record_kernel_reboot_hint() {
  local running latest
  [[ -n "${results_file:-}" ]] || return 0
  running=$(uname -r)
  latest=$(latest_installed_kernel)
  if [[ -n "$latest" && "$running" != "$latest" ]]; then
    echo "reboot_needed:1" >> "$results_file"
    echo "reboot_reason:New kernel $latest installed (still running $running)." >> "$results_file"
  fi
}

# Smarter reboot: kernel + firmware + dnf needs-restarting (cache-only)
record_smart_reboot_hint() {
  [[ -n "${results_file:-}" ]] || return 0
  record_kernel_reboot_hint

  if grep -q '^reboot_needed:1' "$results_file" 2>/dev/null; then
    return 0
  fi

  local nr
  nr=$(timeout 45 dnf needs-restarting -C 2>/dev/null) || true
  if [[ -n "$nr" ]] && ! echo "$nr" | grep -qi 'reboot should not be necessary\|No core libraries'; then
    echo "reboot_needed:1" >> "$results_file"
    echo "reboot_reason:Core libraries/services updated — reboot recommended (dnf needs-restarting)." >> "$results_file"
  fi
}

dnf_history_snippet() {
  local out
  out=$(timeout 20 dnf history info last 2>/dev/null | head -n 50) || true
  [[ -n "$out" ]] || return 0
  if [[ -n "$RUN_LOG_DIR" ]]; then
    printf '%s\n' "$out" > "$RUN_LOG_DIR/dnf-history-last.txt"
  fi
  # Compact for summary: Command + package count lines
  printf '%s\n' "$out" | grep -E '^(Command line|Begin time|Upgrade|Install|Remove|Replaced)' | head -n 20
}

prompt_reboot_if_needed() {
  local reason=""
  [[ -n "${results_file:-}" ]] || return 0
  reason=$(grep '^reboot_reason:' "$results_file" 2>/dev/null | head -1 | cut -d: -f2-) || true
  [[ -n "$reason" ]] || return 0

  if [[ "${AUTO_YES:-0}" -eq 1 ]]; then
    notify "Reboot recommended" "$reason" "critical"
    echo "Reboot recommended: $reason" >&2
    return 0
  fi

  local _ui ask_rc=1
  _ui="$FEDORA_UPDATES_LIB/ui.py"
  notify "Reboot recommended" "$reason" "critical"
  if [[ -n "${DISPLAY:-}" && -f "$_ui" ]] && command -v python3 &>/dev/null; then
    python3 "$_ui" ask --title "Reboot recommended" \
      --text "${reason}"$'\n\n'"Reboot now so the new kernel and modules load?" \
      2>/dev/null && ask_rc=0 || ask_rc=1
  elif command -v zenity &>/dev/null; then
    zenity --question --title="Reboot recommended" \
      --text="${reason}"$'\n\n'"Reboot now so the new kernel and modules load?" \
      --width=480 2>/dev/null && ask_rc=0 || ask_rc=1
  else
    read -rp "Reboot now? [y/N] " _r
    [[ "${_r:-}" =~ ^[yY] ]] && ask_rc=0
  fi

  if [[ $ask_rc -eq 0 ]]; then
    append_summary_log "Reboot requested after updates."
    systemctl reboot || pkexec systemctl reboot || true
  fi
}
