#!/usr/bin/env bash
# UrStack — System Health scan & apply (advanced workstation maintenance)
#
# Status file lines:
#   id|section|title|detail|severity|severity|selected_default|command
#
# Sections: storage | cleanup | workstation | memory | power | advanced | info
# Severity: ok | attention | available | info
# Actionable: 0 | 1
# selected_default: 0 | 1

: "${HEALTH_KEEP_KERNELS:=${KEEP_KERNELS:-3}}"
: "${HEALTH_JOURNAL_VACUUM:=500M}"

_health_emit() { printf '%s\n' "$*"; }
_health_prog() { _health_emit "# $*"; }

_health_clean() {
  local s="${1:-}"
  s="${s//|/\/}"
  s="${s//$'\n'/ }"
  printf '%s' "$s"
}

_health_line() {
  # id section title detail severity actionable selected_default command
  printf '%s|%s|%s|%s|%s|%s|%s|%s\n' \
    "$(_health_clean "$1")" \
    "$(_health_clean "$2")" \
    "$(_health_clean "$3")" \
    "$(_health_clean "$4")" \
    "$(_health_clean "$5")" \
    "$(_health_clean "$6")" \
    "$(_health_clean "$7")" \
    "$(_health_clean "$8")"
}

_health_human_bytes() {
  local b="${1:-0}"
  if command -v numfmt &>/dev/null; then
    numfmt --to=iec --suffix=B "$b" 2>/dev/null && return
  fi
  printf '%s B' "$b"
}

_health_du_bytes() {
  local p="$1"
  [[ -e "$p" ]] || { echo 0; return; }
  du -sb "$p" 2>/dev/null | awk '{print $1}'
}

_health_du_timed() {
  local p="$1" sec="${2:-5}" n=0
  [[ -e "$p" ]] || { echo 0; return; }
  n=$(timeout "$sec" du -sb "$p" 2>/dev/null | awk '{print $1}')
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  echo "$n"
}

# ---------------------------------------------------------------------------
# Storage optimiser — disk meters + known space hogs
# ---------------------------------------------------------------------------
_health_scan_storage() {
  local out="$1"
  local -n _st_lines=$2
  local store="${out}.storage"
  : > "$store"

  _st_fs() {
    local mp="$1" label="$2" id="$3"
    local total=0 used=0 avail=0 pct=0
    read -r total used avail < <(df -B1 -P "$mp" 2>/dev/null | awk 'NR==2{print $2, $3, $4}')
    [[ "$total" =~ ^[0-9]+$ && "$total" -gt 0 ]] || return
    [[ "$used" =~ ^[0-9]+$ ]] || used=0
    [[ "$avail" =~ ^[0-9]+$ ]] || avail=0
    pct=$(( used * 100 / total ))
    printf 'fs|%s|%s|%s|%s|%s|%s\n' "$id" "$label" "$mp" "$used" "$total" "$pct" >> "$store"
    local sev=ok
    if [[ "$mp" == /boot ]]; then
      if [[ "$pct" -ge 80 || "$avail" -lt 209715200 ]]; then
        sev=attention
      elif [[ "$pct" -ge 60 ]]; then
        sev=available
      fi
    elif [[ "$pct" -ge 90 || "$avail" -lt 10737418240 ]]; then
      sev=attention
    elif [[ "$pct" -ge 80 ]]; then
      sev=available
    fi
    _st_lines+=("$(_health_line \
      "$id" "storage" "$label" \
      "$(_health_human_bytes "$used") of $(_health_human_bytes "$total") · ${pct}% used · $(_health_human_bytes "$avail") free" \
      "$sev" "0" "0" "")")
  }

  _st_fs / "Root filesystem (/)" storage-root
  local root_src home_src
  root_src=$(df -P / 2>/dev/null | awk 'NR==2{print $1}')
  home_src=$(df -P /home 2>/dev/null | awk 'NR==2{print $1}')
  if [[ -n "$home_src" && "$home_src" != "$root_src" ]]; then
    _st_fs /home "Home (/home)" storage-home
  fi
  if [[ -d /boot ]]; then
    _st_fs /boot "Boot (/boot)" storage-boot
  fi

  # id|label|path|action-id-or-empty
  local -a hog_spec=(
    "thumbnails|Thumbnail cache|$HOME/.cache/thumbnails|thumbnails"
    "trash|Trash|$HOME/.local/share/Trash|trash"
    "pip-cache|pip cache|$HOME/.cache/pip|pip-cache"
    "npm-cache|npm cache|$HOME/.npm/_cacache|npm-cache"
    "cargo-cache|Cargo registry cache|$HOME/.cargo/registry/cache|cargo-cache"
    "user-cache|User cache (~/.cache)|$HOME/.cache|"
    "flatpak-user|Flatpak app data (~/.var/app)|$HOME/.var/app|"
    "flatpak-user-share|User Flatpak|$HOME/.local/share/flatpak|"
    "podman-user|Podman (user)|$HOME/.local/share/containers|podman-prune"
    "steam|Steam|$HOME/.local/share/Steam|"
    "downloads|Downloads|$HOME/Downloads|"
    "dnf-sys|DNF cache (system)|/var/cache/dnf|dnf-cache"
    "libdnf5-sys|libdnf5 cache (system)|/var/cache/libdnf5|dnf-cache"
    "flatpak-sys|System Flatpak|/var/lib/flatpak|"
    "logs|System logs (/var/log)|/var/log|"
    "var-tmp|Temporary files (/var/tmp)|/var/tmp|"
    "podman-sys|Podman (system)|/var/lib/containers|podman-prune"
    "docker-sys|Docker|/var/lib/docker|docker-prune"
    "snapd|Snap|/var/lib/snapd|snap-old"
    "coredumps|Core dumps|/var/lib/systemd/coredump|coredumps"
    "modules|Kernel modules|/usr/lib/modules|old-kernels"
  )
  [[ -d "$HOME/.steam" ]] && hog_spec+=("steam-dot|Steam|$HOME/.steam|")
  [[ -d "$HOME/.local/share/waydroid" ]] && hog_spec+=("waydroid|Waydroid|$HOME/.local/share/waydroid|")
  [[ -d "$HOME/.cache/libdnf5" ]] && hog_spec+=("libdnf5-user|libdnf5 cache (user)|$HOME/.cache/libdnf5|dnf-cache")

  local spec id label path action b
  local -A seen_path=()
  for spec in "${hog_spec[@]}"; do
    IFS='|' read -r id label path action <<< "$spec"
    [[ -n "$path" && -e "$path" ]] || continue
    [[ -z "${seen_path[$path]:-}" ]] || continue
    seen_path["$path"]=1
    b=$(_health_du_timed "$path" 4)
    [[ "$b" -gt 1048576 ]] || continue
    printf 'hog|%s|%s|%s|%s|%s\n' "$id" "$label" "$path" "$b" "$action" >> "$store"
  done

  # Top-level folders in $HOME (why the disk is full — not all are cleanable)
  local home_line home_path home_b home_name
  if command -v timeout &>/dev/null; then
    while read -r home_b home_path; do
      [[ "$home_path" == "$HOME" ]] && continue
      [[ -n "$home_path" && "$home_b" =~ ^[0-9]+$ ]] || continue
      [[ -z "${seen_path[$home_path]:-}" ]] || continue
      seen_path["$home_path"]=1
      home_name="${home_path##*/}"
      [[ "$home_b" -gt 104857600 ]] || continue
      printf 'hog|home-%s|%s|%s|%s|\n' "$home_name" "$home_name" "$home_path" "$home_b" >> "$store"
    done < <(timeout 10 du -xd1 -b "$HOME" 2>/dev/null | sort -nr | head -16)
  fi

  if [[ -d /var/log/journal ]]; then
    local journal_b
    journal_b=$(_health_du_timed /var/log/journal 3)
    if [[ "$journal_b" -gt 1048576 ]]; then
      printf 'hog|journal|Systemd journal|/var/log/journal|%s|journal-vacuum\n' "$journal_b" >> "$store"
    fi
  fi

  # ── Actionable cleanups (only when there is something to reclaim) ───────
  b=$(_health_du_timed "$HOME/.local/share/Trash" 3)
  if [[ "$b" -gt 10485760 ]]; then
    _st_lines+=("$(_health_line \
      "trash" "storage" "Empty trash" \
      "$(_health_human_bytes "$b") in ~/.local/share/Trash" \
      "available" "1" "0" \
      "gio trash --empty")")
  else
    _st_lines+=("$(_health_line \
      "trash" "storage" "Trash" \
      "Empty or small" \
      "ok" "0" "0" "")")
  fi

  b=$(_health_du_timed "$HOME/.cache/thumbnails" 3)
  if [[ "$b" -gt 20971520 ]]; then
    _st_lines+=("$(_health_line \
      "thumbnails" "storage" "Clear thumbnail cache" \
      "$(_health_human_bytes "$b") in ~/.cache/thumbnails" \
      "available" "1" "0" \
      "rm -rf ~/.cache/thumbnails")")
  else
    _st_lines+=("$(_health_line \
      "thumbnails" "storage" "Thumbnail cache" \
      "$(_health_human_bytes "$b")" \
      "ok" "0" "0" "")")
  fi

  b=$(_health_du_timed "$HOME/.cache/pip" 3)
  if [[ "$b" -gt 52428800 ]]; then
    _st_lines+=("$(_health_line \
      "pip-cache" "storage" "Clear pip cache" \
      "$(_health_human_bytes "$b")" \
      "available" "1" "0" \
      "pip cache purge")")
  fi

  b=$(_health_du_timed "$HOME/.npm/_cacache" 3)
  if [[ "$b" -eq 0 ]]; then
    b=$(_health_du_timed "$HOME/.local/share/npm/_cacache" 3)
  fi
  if [[ "$b" -gt 52428800 ]]; then
    _st_lines+=("$(_health_line \
      "npm-cache" "storage" "Clear npm cache" \
      "$(_health_human_bytes "$b")" \
      "available" "1" "0" \
      "npm cache clean --force")")
  fi

  b=$(_health_du_timed "$HOME/.cargo/registry/cache" 3)
  if [[ "$b" -gt 104857600 ]]; then
    _st_lines+=("$(_health_line \
      "cargo-cache" "storage" "Clear Cargo registry cache" \
      "$(_health_human_bytes "$b") — downloaded crate sources, not ~/.cargo/bin" \
      "available" "1" "0" \
      "rm -rf ~/.cargo/registry/cache")")
  fi

  local auto_n=0
  if command -v dnf &>/dev/null || command -v dnf5 &>/dev/null; then
    auto_n=$(timeout 8 dnf repoquery --unneeded -C --qf '%{name}\n' 2>/dev/null | grep -c . || true)
    [[ "$auto_n" =~ ^[0-9]+$ ]] || auto_n=0
  fi
  if [[ "$auto_n" -gt 0 ]]; then
    _st_lines+=("$(_health_line \
      "dnf-autoremove" "storage" "Remove unused DNF packages" \
      "$auto_n leftover package(s) · dnf autoremove" \
      "available" "1" "0" \
      "dnf autoremove -y")")
  else
    _st_lines+=("$(_health_line \
      "dnf-autoremove" "storage" "Unused DNF packages" \
      "None listed" \
      "ok" "0" "0" "")")
  fi

  if command -v podman &>/dev/null; then
    b=$(( $(_health_du_timed "$HOME/.local/share/containers" 4) + $(_health_du_timed /var/lib/containers 4) ))
    if [[ "$b" -gt 209715200 ]]; then
      _st_lines+=("$(_health_line \
        "podman-prune" "storage" "Prune unused Podman data" \
        "$(_health_human_bytes "$b") in container storage · unused images/containers" \
        "available" "1" "0" \
        "podman system prune -af")")
    else
      _st_lines+=("$(_health_line \
        "podman-prune" "storage" "Podman storage" \
        "$(_health_human_bytes "$b")" \
        "ok" "0" "0" "")")
    fi
  fi

  if command -v docker &>/dev/null; then
    b=$(_health_du_timed /var/lib/docker 4)
    if [[ "$b" -gt 209715200 ]]; then
      _st_lines+=("$(_health_line \
        "docker-prune" "storage" "Prune unused Docker data" \
        "$(_health_human_bytes "$b") in /var/lib/docker" \
        "available" "1" "0" \
        "docker system prune -af")")
    fi
  fi

  b=$(_health_du_timed /var/lib/systemd/coredump 3)
  if [[ "$b" -gt 1048576 ]]; then
    _st_lines+=("$(_health_line \
      "coredumps" "storage" "Delete core dumps" \
      "$(_health_human_bytes "$b") in /var/lib/systemd/coredump" \
      "available" "1" "0" \
      "rm core dumps")")
  else
    _st_lines+=("$(_health_line \
      "coredumps" "storage" "Core dumps" \
      "None of note" \
      "ok" "0" "0" "")")
  fi

  if command -v snap &>/dev/null; then
    local snap_old
    snap_old=$(snap list --all 2>/dev/null | awk 'NR>1 && /disabled/{c++} END{print c+0}')
    if [[ "${snap_old:-0}" -gt 0 ]]; then
      _st_lines+=("$(_health_line \
        "snap-old" "storage" "Remove old Snap revisions" \
        "$snap_old disabled revision(s)" \
        "available" "1" "0" \
        "snap remove --revision …")")
    else
      _st_lines+=("$(_health_line \
        "snap-old" "storage" "Snap revisions" \
        "No disabled revisions" \
        "ok" "0" "0" "")")
    fi
  fi
}

# Persisted one-shot / recently-applied markers so suggestions clear after apply
_health_applied_file() {
  echo "${XDG_STATE_HOME:-$HOME/.local/state}/urstack/health-applied.conf"
}

_health_mark_applied() {
  local id="$1"
  local f now tmp
  [[ -n "$id" ]] || return 0
  f=$(_health_applied_file)
  mkdir -p "$(dirname "$f")"
  now=$(date -Iseconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S)
  tmp=$(mktemp)
  if [[ -f "$f" ]]; then
    grep -v "^${id}=" "$f" > "$tmp" 2>/dev/null || true
  fi
  printf '%s=%s\n' "$id" "$now" >> "$tmp"
  mv -f "$tmp" "$f"
}

_health_applied_epoch() {
  local id="$1" f val
  f=$(_health_applied_file)
  [[ -f "$f" ]] || { echo 0; return; }
  val=$(grep "^${id}=" "$f" 2>/dev/null | head -1 | cut -d= -f2-)
  [[ -n "$val" ]] || { echo 0; return; }
  date -d "$val" +%s 2>/dev/null || echo 0
}

# Days since id was applied; 9999 if never
_health_applied_age_days() {
  local id="$1" epoch now age
  epoch=$(_health_applied_epoch "$id")
  [[ "$epoch" =~ ^[0-9]+$ && "$epoch" -gt 0 ]] || { echo 9999; return; }
  now=$(date +%s)
  age=$(( (now - epoch) / 86400 ))
  [[ "$age" -lt 0 ]] && age=0
  echo "$age"
}

# Power profiles: GNOME uses power-profiles-daemon (powerprofilesctl);
# Fedora KDE uses tuned-ppd, which exposes the same D-Bus API but not the CLI.
_PPD_BUS_DEST=org.freedesktop.UPower.PowerProfiles
_PPD_BUS_PATH=/org/freedesktop/UPower/PowerProfiles
_PPD_BUS_IFACE=org.freedesktop.UPower.PowerProfiles

_ppd_available() {
  command -v powerprofilesctl &>/dev/null && return 0
  busctl get-property "$_PPD_BUS_DEST" "$_PPD_BUS_PATH" "$_PPD_BUS_IFACE" ActiveProfile &>/dev/null
}

_ppd_get() {
  local v
  if command -v powerprofilesctl &>/dev/null; then
    v=$(powerprofilesctl get 2>/dev/null) && { printf '%s\n' "$v"; return 0; }
  fi
  v=$(busctl get-property "$_PPD_BUS_DEST" "$_PPD_BUS_PATH" "$_PPD_BUS_IFACE" ActiveProfile 2>/dev/null) || return 1
  v="${v#*\"}"
  v="${v%\"*}"
  [[ -n "$v" ]] || return 1
  printf '%s\n' "$v"
}

_ppd_set() {
  local prof="$1"
  [[ -n "$prof" ]] || return 1
  if command -v powerprofilesctl &>/dev/null; then
    powerprofilesctl set "$prof"
    return
  fi
  busctl set-property "$_PPD_BUS_DEST" "$_PPD_BUS_PATH" "$_PPD_BUS_IFACE" ActiveProfile s "$prof"
}

# Self-contained: user apply steps run via bash -lc, not this file's functions.
_ppd_set_cmd() {
  local p="$1"
  printf 'if command -v powerprofilesctl >/dev/null 2>&1; then powerprofilesctl set %s; else busctl set-property org.freedesktop.UPower.PowerProfiles /org/freedesktop/UPower/PowerProfiles org.freedesktop.UPower.PowerProfiles ActiveProfile s %s; fi' "$p" "$p"
}

# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
fedora_health_scan() {
  local out="${1:-}"
  [[ -n "$out" ]] || { echo "usage: fedora_health_scan <status-file>" >&2; return 2; }
  : > "$out"
  HEALTH_KEEP_KERNELS="${KEEP_KERNELS:-${HEALTH_KEEP_KERNELS:-3}}"

  local -a lines=()
  local tmp
  tmp=$(mktemp)

  _health_scan_storage "$out" lines

  # ── Cleanup: old kernels ────────────────────────────────────────────────
  local keep="${HEALTH_KEEP_KERNELS}"
  local running
  running=$(uname -r)
  mapfile -t _allk < <(rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort -V)
  local n_kern=${#_allk[@]}
  local -a _remove_kern=()
  if [[ $n_kern -gt $keep ]]; then
    local -A _keep_set=()
    _keep_set["$running"]=1
    local i count=1
    for (( i=n_kern-1; i>=0; i-- )); do
      [[ -n "${_keep_set[${_allk[i]}]:-}" ]] && continue
      _keep_set["${_allk[i]}"]=1
      count=$((count + 1))
      [[ $count -ge $keep ]] && break
    done
    local k
    for k in "${_allk[@]}"; do
      [[ -n "${_keep_set[$k]:-}" ]] && continue
      _remove_kern+=("$k")
    done
  fi
  if [[ ${#_remove_kern[@]} -gt 0 ]]; then
    lines+=("$(_health_line \
      "old-kernels" "cleanup" "Remove old kernels" \
      "${#_remove_kern[@]} removable · keep $keep including running $running · ${_remove_kern[*]}" \
      "attention" "1" "0" \
      "dnf remove old kernel packages (keep $keep)")")
  else
    lines+=("$(_health_line \
      "old-kernels" "cleanup" "Old kernels" \
      "$n_kern installed · keep policy $keep · running $running" \
      "ok" "0" "0" "")")
  fi

  # ── Cleanup: DNF cache (system + per-user libdnf5) ──────────────────────
  local dnf_cache_b=0
  local c
  for c in /var/cache/dnf /var/cache/libdnf5 "$HOME/.cache/libdnf5" "$HOME/.cache/dnf"; do
    dnf_cache_b=$((dnf_cache_b + $(_health_du_bytes "$c")))
  done
  local dnf_clean_age
  dnf_clean_age=$(_health_applied_age_days "dnf-cache")
  if [[ "$dnf_cache_b" -gt 104857600 && "$dnf_clean_age" -ge 1 ]]; then
    lines+=("$(_health_line \
      "dnf-cache" "cleanup" "Clean DNF cache" \
      "$(_health_human_bytes "$dnf_cache_b") in package caches (system + ~/.cache)" \
      "available" "1" "0" \
      "dnf clean all + clear ~/.cache/libdnf5")")
  elif [[ "$dnf_cache_b" -gt 104857600 && "$dnf_clean_age" -lt 1 ]]; then
    lines+=("$(_health_line \
      "dnf-cache" "cleanup" "DNF cache" \
      "Cleaned recently — $(_health_human_bytes "$dnf_cache_b") may refill after metadata sync" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "dnf-cache" "cleanup" "DNF cache" \
      "$(_health_human_bytes "$dnf_cache_b") — already small" \
      "ok" "0" "0" "")")
  fi

  # ── Cleanup: journal ────────────────────────────────────────────────────
  local journal_raw journal_h
  journal_raw=$(journalctl --disk-usage 2>/dev/null | head -1 || true)
  journal_h="${journal_raw:-unknown}"
  local journal_mib
  journal_mib=$(echo "$journal_raw" | grep -oE '[0-9]+(\.[0-9]+)?[KMGT]?B' | head -1 || true)
  # Heuristic: offer vacuum if usage string mentions G or large M
  if echo "$journal_raw" | grep -qiE '[0-9]+(\.[0-9]+)?G|[5-9][0-9][0-9]M|[0-9]+\.[0-9]+G'; then
    lines+=("$(_health_line \
      "journal-vacuum" "cleanup" "Vacuum systemd journal" \
      "$journal_h → target ${HEALTH_JOURNAL_VACUUM}" \
      "attention" "1" "0" \
      "journalctl --vacuum-size=${HEALTH_JOURNAL_VACUUM}")")
  else
    lines+=("$(_health_line \
      "journal-vacuum" "cleanup" "Systemd journal" \
      "$journal_h" \
      "ok" "0" "0" "")")
  fi

  # ── Cleanup: flatpak unused ─────────────────────────────────────────────
  if command -v flatpak &>/dev/null; then
    local unused
    unused=$(flatpak uninstall --unused --assumeyes --dry-run 2>/dev/null | grep -E '^(Uninstalling|Nothing)' || true)
    if echo "$unused" | grep -qi uninstalling; then
      local ucount
      ucount=$(echo "$unused" | grep -ci uninstalling || true)
      lines+=("$(_health_line \
        "flatpak-unused" "cleanup" "Remove unused Flatpak runtimes" \
        "${ucount:-some} unused ref(s) · flatpak uninstall --unused" \
        "available" "1" "0" \
        "flatpak uninstall --unused -y")")
    else
      lines+=("$(_health_line \
        "flatpak-unused" "cleanup" "Unused Flatpak runtimes" \
        "None pending" \
        "ok" "0" "0" "")")
    fi

    # Orphan ~/.var/app data for apps no longer installed
    local -a orphans=()
    local appdir app_id
    if [[ -d "$HOME/.var/app" ]]; then
      for appdir in "$HOME/.var/app"/*; do
        [[ -d "$appdir" ]] || continue
        app_id=$(basename "$appdir")
        if ! flatpak info "$app_id" &>/dev/null \
          && ! flatpak info --user "$app_id" &>/dev/null \
          && ! flatpak info --system "$app_id" &>/dev/null; then
          orphans+=("$app_id")
        fi
      done
    fi
    if [[ ${#orphans[@]} -gt 0 ]]; then
      lines+=("$(_health_line \
        "flatpak-orphans" "cleanup" "Delete orphan Flatpak user data" \
        "${#orphans[@]} dir(s) under ~/.var/app · ${orphans[*]}" \
        "attention" "1" "0" \
        "rm -rf ~/.var/app/<orphaned ids>")")
      printf '%s\n' "${orphans[@]}" > "${out}.flatpak-orphans"
    else
      lines+=("$(_health_line \
        "flatpak-orphans" "cleanup" "Flatpak user data orphans" \
        "No orphan dirs in ~/.var/app" \
        "ok" "0" "0" "")")
    fi
  fi

  # ── Workstation: Flathub ────────────────────────────────────────────────
  if command -v flatpak &>/dev/null; then
    if flatpak remotes --columns=name 2>/dev/null | grep -qx flathub; then
      lines+=("$(_health_line \
        "flathub" "workstation" "Flathub remote" \
        "Present" \
        "ok" "0" "0" "")")
    else
      lines+=("$(_health_line \
        "flathub" "workstation" "Add Flathub remote" \
        "Missing — required for most catalog Flatpaks" \
        "attention" "1" "1" \
        "flatpak remote-add --if-not-exists flathub …")")
    fi
  fi

  # ── Workstation: RPM Fusion ─────────────────────────────────────────────
  local have_rf_free=0 have_rf_nonfree=0
  [[ -f /etc/yum.repos.d/rpmfusion-free.repo ]] && have_rf_free=1
  [[ -f /etc/yum.repos.d/rpmfusion-nonfree.repo ]] && have_rf_nonfree=1
  if [[ $have_rf_free -eq 1 && $have_rf_nonfree -eq 1 ]]; then
    lines+=("$(_health_line \
      "rpmfusion" "workstation" "RPM Fusion" \
      "free + nonfree repos present" \
      "ok" "0" "0" "")")
  else
    local missing=""
    [[ $have_rf_free -eq 0 ]] && missing+="free "
    [[ $have_rf_nonfree -eq 0 ]] && missing+="nonfree "
    lines+=("$(_health_line \
      "rpmfusion" "workstation" "Enable RPM Fusion" \
      "Missing: ${missing}· installs rpmfusion-*-release for this Fedora" \
      "available" "1" "0" \
      "dnf install rpmfusion-free-release rpmfusion-nonfree-release")")
  fi

  # ── Workstation: codecs ─────────────────────────────────────────────────
  local -a codec_pkgs=(ffmpeg gstreamer1-plugin-libav gstreamer1-plugins-ugly gstreamer1-plugins-bad-free)
  local -a missing_codecs=()
  local p
  for p in "${codec_pkgs[@]}"; do
    rpm -q "$p" &>/dev/null || missing_codecs+=("$p")
  done
  if [[ ${#missing_codecs[@]} -gt 0 ]]; then
    lines+=("$(_health_line \
      "codecs" "workstation" "Install multimedia codecs" \
      "Missing: ${missing_codecs[*]} · swaps Fedora ffmpeg-free for RPM Fusion ffmpeg" \
      "available" "1" "0" \
      "dnf install ${missing_codecs[*]}")")
  else
    lines+=("$(_health_line \
      "codecs" "workstation" "Multimedia codecs" \
      "Core set installed (${codec_pkgs[*]})" \
      "ok" "0" "0" "")")
  fi

  # ── Workstation: firmware note ──────────────────────────────────────────
  if command -v fwupdmgr &>/dev/null; then
    local fw_out
    fw_out=$(timeout "${TIMEOUT_FW:-20}" fwupdmgr get-updates 2>/dev/null) || true
    if declare -F fwupd_output_has_updates &>/dev/null && fwupd_output_has_updates "$fw_out"; then
      lines+=("$(_health_line \
        "firmware-note" "workstation" "Firmware updates available" \
        "Apply from Updates (fwupd) — not duplicated here" \
        "info" "0" "0" "")")
    else
      lines+=("$(_health_line \
        "firmware-note" "workstation" "Firmware (fwupd)" \
        "No pending updates reported" \
        "ok" "0" "0" "")")
    fi
  fi

  # ── Memory: zram / swap ─────────────────────────────────────────────────
  local zram_on=0
  if command -v zramctl &>/dev/null && zramctl --output NAME --noheadings 2>/dev/null | grep -q .; then
    zram_on=1
  elif swapon --show=NAME --noheadings 2>/dev/null | grep -q zram; then
    zram_on=1
  fi
  local swap_line
  swap_line=$(swapon --show --noheadings 2>/dev/null | tr '\n' '; ' || echo none)
  if [[ $zram_on -eq 1 ]]; then
    lines+=("$(_health_line \
      "zram" "memory" "zram swap" \
      "Active · ${swap_line:-ok}" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "zram" "memory" "Enable zram-generator" \
      "No zram device · install/enable systemd-zram-generator defaults" \
      "available" "1" "0" \
      "dnf install zram-generator-defaults && systemctl daemon-reload")")
  fi

  # ── Memory: earlyoom ────────────────────────────────────────────────────
  if systemctl is-enabled earlyoom &>/dev/null || systemctl is-active earlyoom &>/dev/null; then
    lines+=("$(_health_line \
      "earlyoom" "memory" "EarlyOOM" \
      "enabled/active — kills the worst offender under memory pressure" \
      "ok" "0" "0" "")")
  elif rpm -q earlyoom &>/dev/null; then
    lines+=("$(_health_line \
      "earlyoom" "memory" "Enable EarlyOOM" \
      "Installed but not enabled" \
      "available" "1" "0" \
      "systemctl enable --now earlyoom")")
  else
    lines+=("$(_health_line \
      "earlyoom" "memory" "Install & enable EarlyOOM" \
      "Not installed · recommended on low-RAM or heavy desktop workloads" \
      "available" "1" "0" \
      "dnf install earlyoom && systemctl enable --now earlyoom")")
  fi

  # ── Memory: boot blame (info) ───────────────────────────────────────────
  if command -v systemd-analyze &>/dev/null; then
    local blame_time blame_full
    blame_time=$(systemd-analyze 2>/dev/null | head -1 || true)
    {
      echo "${blame_time:-Boot timing unavailable}"
      echo
      echo "Slowest units"
      echo "─────────────"
      timeout 8 systemd-analyze blame 2>/dev/null | head -25
      echo
      echo "Critical chain"
      echo "──────────────"
      timeout 8 systemd-analyze critical-chain 2>/dev/null | head -30
    } > "${out}.boot-blame" 2>/dev/null || true
    lines+=("$(_health_line \
      "boot-blame" "memory" "Boot analysis" \
      "${blame_time:-See details} · tap to open the full report" \
      "info" "0" "0" "")")
  fi

  # ── Power ───────────────────────────────────────────────────────────────
  if _ppd_available; then
    local cur_prof sev_b act_b sev_p act_p sev_s act_s det_b det_p det_s
    cur_prof=$(_ppd_get 2>/dev/null || echo unknown)
    # Mutually exclusive: the active profile is healthy. The others are
    # switchers (severity "choice"), not missing fixes — otherwise Health
    # stays "degraded" no matter which profile you pick.
    sev_b=choice; act_b=1; sev_p=choice; act_p=1; sev_s=choice; act_s=1
    det_b="Currently $cur_prof — apply to switch"
    det_p="Currently $cur_prof — apply to switch"
    det_s="Currently $cur_prof — apply to switch"
    [[ "$cur_prof" == balanced ]] && { sev_b=ok; act_b=0; det_b="Active now"; }
    [[ "$cur_prof" == performance ]] && { sev_p=ok; act_p=0; det_p="Active now"; }
    [[ "$cur_prof" == power-saver ]] && { sev_s=ok; act_s=0; det_s="Active now"; }
    lines+=("$(_health_line \
      "power-balanced" "power" "Power profile → balanced" \
      "$det_b" "$sev_b" "$act_b" "0" \
      "$(_ppd_set_cmd balanced)")")
    lines+=("$(_health_line \
      "power-performance" "power" "Power profile → performance" \
      "$det_p" "$sev_p" "$act_p" "0" \
      "$(_ppd_set_cmd performance)")")
    lines+=("$(_health_line \
      "power-saver" "power" "Power profile → power-saver" \
      "$det_s" "$sev_s" "$act_s" "0" \
      "$(_ppd_set_cmd power-saver)")")
  else
    local ppd_title="Install power profiles" ppd_detail
    if rpm -q tuned-ppd &>/dev/null || rpm -q power-profiles-daemon &>/dev/null; then
      ppd_title="Enable power profiles"
      ppd_detail="Power profiles service is installed but not running"
    else
      ppd_detail="No power-profiles provider (tuned-ppd or power-profiles-daemon)"
    fi
    lines+=("$(_health_line \
      "power-ppd" "power" "$ppd_title" \
      "$ppd_detail · needed for balanced / performance / power-saver" \
      "available" "1" "0" \
      "enable tuned-ppd or power-profiles-daemon")")
  fi

  local tlp_on=0 ppd_on=0
  systemctl is-enabled tlp &>/dev/null && tlp_on=1
  systemctl is-active tlp &>/dev/null && tlp_on=1
  systemctl is-enabled power-profiles-daemon &>/dev/null && ppd_on=1
  systemctl is-active power-profiles-daemon &>/dev/null && ppd_on=1
  systemctl is-enabled tuned-ppd &>/dev/null && ppd_on=1
  systemctl is-active tuned-ppd &>/dev/null && ppd_on=1
  _ppd_available && ppd_on=1
  if [[ $tlp_on -eq 1 && $ppd_on -eq 1 ]]; then
    lines+=("$(_health_line \
      "tlp-conflict" "power" "Disable TLP (conflicts with ppd)" \
      "Both TLP and a power-profiles service are active — pick one" \
      "attention" "1" "0" \
      "systemctl disable --now tlp")")
  elif [[ $tlp_on -eq 1 ]]; then
    lines+=("$(_health_line \
      "tlp-conflict" "power" "TLP" \
      "TLP active (ppd not) — OK if intentional" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "tlp-conflict" "power" "TLP vs power profiles" \
      "No conflict detected" \
      "ok" "0" "0" "")")
  fi

  # ── Advanced ────────────────────────────────────────────────────────────
  local fstrim_age
  fstrim_age=$(_health_applied_age_days "fstrim")
  if [[ "$fstrim_age" -lt 14 ]]; then
    lines+=("$(_health_line \
      "fstrim" "advanced" "SSD trim" \
      "Ran recently (${fstrim_age}d ago) — next suggestion in $((14 - fstrim_age))d" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "fstrim" "advanced" "Trim SSD once (fstrim -av)" \
      "Runs discard on mounted filesystems that support it" \
      "available" "1" "0" \
      "fstrim -av")")
  fi

  if [[ -f /etc/dnf/dnf.conf.d/99-urstack-speed.conf ]] || [[ -f /etc/dnf/libdnf5.conf.d/99-urstack-speed.conf ]]; then
    lines+=("$(_health_line \
      "dnf-speed" "advanced" "DNF parallel downloads" \
      "UrStack drop-in already present" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "dnf-speed" "advanced" "Speed up DNF downloads" \
      "Write max_parallel_downloads=10 + fastestmirror drop-in" \
      "available" "1" "0" \
      "write /etc/dnf/dnf.conf.d/99-urstack-speed.conf")")
  fi

  if [[ -f /etc/sysctl.d/99-urstack.conf ]]; then
    lines+=("$(_health_line \
      "sysctl" "advanced" "Sysctl tuning drop-in" \
      "/etc/sysctl.d/99-urstack.conf present" \
      "ok" "0" "0" "")")
  else
    lines+=("$(_health_line \
      "sysctl" "advanced" "Apply sysctl snips" \
      "vm.swappiness=10 · fs.inotify.max_user_watches=524288" \
      "available" "1" "0" \
      "write /etc/sysctl.d/99-urstack.conf && sysctl --system")")
  fi

  # User services — only truly *enabled* units (not static/disabled/not-found)
  local -a heavy_units=(tracker-miner-fs-3.service evolution-addressbook-factory.service)
  local u ustate
  for u in "${heavy_units[@]}"; do
    ustate=$(systemctl --user is-enabled "$u" 2>/dev/null || true)
    if [[ "$ustate" == "enabled" ]]; then
      lines+=("$(_health_line \
        "userunit-$u" "advanced" "Disable user unit: $u" \
        "Currently enabled for your user" \
        "available" "1" "0" \
        "systemctl --user disable --now $u")")
    fi
  done

  printf '%s\n' "${lines[@]}" > "$out"
  rm -f "$tmp"
  return 0
}

# ---------------------------------------------------------------------------
# Restore points (safety net before Health apply)
# ---------------------------------------------------------------------------
_health_rp_root() {
  local d="${XDG_STATE_HOME:-$HOME/.local/state}/urstack/health-restore-points"
  mkdir -p "$d"
  echo "$d"
}

_health_dnf_history_id() {
  dnf history list 2>/dev/null | awk 'NR>2 && $1 ~ /^[0-9]+$/ {print $1; exit}'
}

_health_snapshot_file() {
  local src="$1" dest_dir="$2" name="$3"
  if [[ -f "$src" ]]; then
    cp -a "$src" "$dest_dir/$name" 2>/dev/null || true
    echo "present" > "$dest_dir/$name.state"
  else
    echo "missing" > "$dest_dir/$name.state"
  fi
}

# Create a restore point. Prints DEST=<path> and RESTORE_POINT=<id>
fedora_health_restore_point_create() {
  local reason="${1:-manual}"
  local root id dest files
  root=$(_health_rp_root)
  id=$(date +%Y%m%d-%H%M%S)
  dest="$root/$id"
  files="$dest/files"
  mkdir -p "$files" "$dest/state"
  mark_tree_incomplete "$dest" "restore point"

  _health_prog "Creating restore point $id…"
  echo "10"

  local hist dnf_rollback=0
  hist=$(_health_dnf_history_id)
  case "$reason" in
    *old-kernels*|*rpmfusion*|*codecs*|*zram*|*earlyoom*|*power-ppd*) dnf_rollback=1 ;;
  esac
  {
    echo "id=$id"
    echo "created=$(date -Iseconds)"
    echo "reason=$reason"
    echo "dnf_history_id=${hist:-0}"
    echo "dnf_rollback=$dnf_rollback"
    echo "hostname=$(hostname 2>/dev/null || echo unknown)"
    echo "kernel=$(uname -r)"
  } > "$dest/meta.conf"

  rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort > "$dest/state/rpm-qa.txt" || true
  flatpak list --columns=application,version 2>/dev/null > "$dest/state/flatpak.txt" || true
  swapon --show 2>/dev/null > "$dest/state/swapon.txt" || true
  _ppd_get 2>/dev/null > "$dest/state/power-profile.txt" || true
  systemctl is-enabled earlyoom 2>/dev/null > "$dest/state/earlyoom.enabled" || echo "unknown" > "$dest/state/earlyoom.enabled"
  systemctl is-enabled tlp 2>/dev/null > "$dest/state/tlp.enabled" || echo "unknown" > "$dest/state/tlp.enabled"
  systemctl --user list-unit-files --state=enabled --no-pager 2>/dev/null > "$dest/state/user-units-enabled.txt" || true

  _health_snapshot_file /etc/sysctl.d/99-urstack.conf "$files" "99-urstack.conf"
  _health_snapshot_file /etc/dnf/dnf.conf.d/99-urstack-speed.conf "$files" "99-urstack-speed-dnf.conf"
  _health_snapshot_file /etc/dnf/libdnf5.conf.d/99-urstack-speed.conf "$files" "99-urstack-speed-libdnf5.conf"

  # Keep only the newest 5 points
  mapfile -t _all_rp < <(ls -1 "$root" 2>/dev/null | sort -r)
  local i
  for (( i=5; i<${#_all_rp[@]}; i++ )); do
    rm -rf "${root:?}/${_all_rp[i]:?}"
  done

  mark_tree_complete "$dest"
  echo "100"
  _health_prog "Restore point ready: $id"
  echo "DEST=$dest"
  echo "RESTORE_POINT=$id"
  return 0
}

fedora_health_restore_point_latest() {
  local root id
  root=$(_health_rp_root)
  # An interrupted point sorts newest but cannot be restored from. Returning it
  # would hide the last good one behind something unusable.
  for id in $(ls -1 "$root" 2>/dev/null | sort -r); do
    tree_is_incomplete "$root/$id" && continue
    printf '%s\n' "$id"
    return 0
  done
}

fedora_health_restore_point_list() {
  local root id meta created reason
  root=$(_health_rp_root)
  for id in $(ls -1 "$root" 2>/dev/null | sort -r); do
    meta="$root/$id/meta.conf"
    created=""; reason=""
    [[ -f "$meta" ]] && {
      created=$(grep '^created=' "$meta" | head -1 | cut -d= -f2-)
      reason=$(grep '^reason=' "$meta" | head -1 | cut -d= -f2-)
    }
    # Listed rather than hidden: the folder is on disk either way, so say why
    # it cannot be used instead of leaving it unexplained.
    if tree_is_incomplete "$root/$id"; then
      reason="[incomplete — cannot restore] ${reason:-}"
    fi
    printf '%s|%s|%s\n' "$id" "${created:-}" "${reason:-}"
  done
}

# Restore from a point id (or latest)
fedora_health_restore_point_apply() {
  local id="${1:-latest}"
  local root dest hist
  root=$(_health_rp_root)
  if [[ "$id" == "latest" || -z "$id" ]]; then
    id=$(fedora_health_restore_point_latest)
  fi
  dest="$root/$id"
  [[ -d "$dest" ]] || { _health_prog "No restore point: $id"; return 1; }
  # Partial points restore whichever files were captured before the interruption
  # and silently skip the rest, which looks like a successful rollback.
  if tree_is_incomplete "$dest"; then
    _health_prog "Restore point $id never finished — refusing to restore from it."
    return 1
  fi

  export URSTACK_EMBEDDED_PROGRESS="${URSTACK_EMBEDDED_PROGRESS:-1}"
  _health_prog "Restoring from $id…"
  echo "5"

  hist=$(grep '^dnf_history_id=' "$dest/meta.conf" 2>/dev/null | cut -d= -f2)
  local dnf_rollback=""
  dnf_rollback=$(grep '^dnf_rollback=' "$dest/meta.conf" 2>/dev/null | cut -d= -f2)
  # Legacy points (no flag) keep the old rollback behaviour
  [[ -n "$dnf_rollback" ]] || dnf_rollback=1
  local jobs
  jobs=$(mktemp)
  {
    echo "health_restore_files $dest"
    if [[ "$dnf_rollback" == "1" && -n "$hist" && "$hist" != "0" ]]; then
      echo "dnf_history_rollback $hist"
    fi
    if [[ -f "$dest/state/earlyoom.enabled" ]]; then
      local ee
      ee=$(tr -d '[:space:]' < "$dest/state/earlyoom.enabled")
      echo "unit_set_enabled earlyoom $ee"
    fi
    if [[ -f "$dest/state/tlp.enabled" ]]; then
      local te
      te=$(tr -d '[:space:]' < "$dest/state/tlp.enabled")
      echo "unit_set_enabled tlp $te"
    fi
  } > "$jobs"

  _health_prog "Rolling back system changes (polkit)…"
  echo "30"
  local ec=0
  _health_run_priv_jobs "$jobs" || ec=1
  rm -f "$jobs"
  echo "70"

  if [[ -f "$dest/state/power-profile.txt" ]]; then
    local pp
    pp=$(tr -d '[:space:]' < "$dest/state/power-profile.txt")
    [[ -n "$pp" && "$pp" != "unknown" ]] && _ppd_set "$pp" 2>/dev/null || true
  fi

  echo "100"
  if [[ $ec -eq 0 ]]; then
    _health_prog "Restore point $id applied"
  else
    _health_prog "Restore finished with errors — check log"
  fi
  echo "RESTORE_POINT=$id"
  return "$ec"
}

# ---------------------------------------------------------------------------
# Privileged batch helper (via pkexec priv.sh jobs)
# ---------------------------------------------------------------------------
_health_run_priv_jobs() {
  local jobs_file="$1"
  local priv_helper
  priv_helper=$(priv_helper_path 2>/dev/null || echo "${FEDORA_UPDATES_LIB:-}/priv.sh")
  [[ -x "$priv_helper" || -f "$priv_helper" ]] || {
    _health_prog "Missing priv.sh"
    return 1
  }
  KEEP_KERNELS="${HEALTH_KEEP_KERNELS:-${KEEP_KERNELS:-3}}" \
    HEALTH_JOURNAL_VACUUM="${HEALTH_JOURNAL_VACUUM}" \
    priv_jobs_inject_env "$jobs_file"
  pkexec_priv "$jobs_file"
}

# ---------------------------------------------------------------------------
# Apply selected ids
# ---------------------------------------------------------------------------
fedora_health_apply() {
  local ids_csv="${1:-}"
  local status_file="${2:-${URSTACK_HEALTH_STATUS:-}}"
  [[ -n "$ids_csv" ]] || { echo "usage: fedora_health_apply id1,id2 […] [status-file]" >&2; return 2; }
  HEALTH_KEEP_KERNELS="${KEEP_KERNELS:-${HEALTH_KEEP_KERNELS:-3}}"

  export URSTACK_EMBEDDED_PROGRESS="${URSTACK_EMBEDDED_PROGRESS:-1}"

  IFS=',' read -r -a ids <<< "$ids_csv"
  local -A want=()
  local id
  for id in "${ids[@]}"; do
    id="${id// /}"
    [[ -n "$id" ]] && want["$id"]=1
  done

  local orphans_file=""
  [[ -n "$status_file" && -f "${status_file}.flatpak-orphans" ]] && orphans_file="${status_file}.flatpak-orphans"

  # Safety net: restore point before changing the system (default on).
  # A failure here usually means a full disk or unreadable DNF history — exactly
  # the conditions where the destructive steps below must not run. Abort instead.
  if [[ "${URSTACK_HEALTH_SKIP_RESTORE_POINT:-0}" != "1" ]]; then
    _health_prog "Creating restore point before apply…"
    echo "3"
    if ! fedora_health_restore_point_create "before-apply:${ids_csv:0:80}"; then
      _health_prog "ERROR: could not create a restore point — aborting before any changes."
      _health_prog "Set URSTACK_HEALTH_SKIP_RESTORE_POINT=1 to apply without one."
      echo "100"
      return 1
    fi
  fi

  _health_prog "Applying ${#want[@]} health action(s)…"
  echo "5"

  local jobs
  jobs=$(mktemp)
  local -a user_cmds=()
  local step=0 total=${#want[@]}
  [[ "$total" -lt 1 ]] && total=1

  _queue_priv() { printf '%s\n' "$1" >> "$jobs"; }
  _queue_user() { user_cmds+=("$1"); }

  for id in "${!want[@]}"; do
    case "$id" in
      old-kernels) _queue_priv "prune_old_kernels" ;;
      dnf-cache)
        _queue_priv "dnf_clean_all"
        # Most cache on modern Fedora is per-user under ~/.cache/libdnf5
        _queue_user 'rm -rf "$HOME/.cache/libdnf5" "$HOME/.cache/dnf"; (command -v dnf5 >/dev/null && dnf5 clean all) || dnf clean all || true'
        ;;
      dnf-autoremove) _queue_priv "dnf_autoremove" ;;
      trash) _queue_user 'gio trash --empty 2>/dev/null || rm -rf "$HOME/.local/share/Trash/files"/* "$HOME/.local/share/Trash/info"/*' ;;
      thumbnails) _queue_user 'rm -rf "$HOME/.cache/thumbnails"' ;;
      pip-cache) _queue_user 'pip cache purge 2>/dev/null || pip3 cache purge 2>/dev/null || rm -rf "$HOME/.cache/pip"' ;;
      npm-cache) _queue_user 'npm cache clean --force 2>/dev/null || rm -rf "$HOME/.npm/_cacache" "$HOME/.local/share/npm/_cacache"' ;;
      cargo-cache) _queue_user 'rm -rf "$HOME/.cargo/registry/cache" "$HOME/.cargo/git/db"' ;;
      podman-prune) _queue_user 'podman system prune -af' ;;
      docker-prune) _queue_user 'docker system prune -af' ;;
      coredumps) _queue_priv "coredump_vacuum" ;;
      snap-old) _queue_priv "snap_purge_old" ;;
      journal-vacuum) _queue_priv "journal_vacuum" ;;
      flatpak-unused) _queue_user "flatpak uninstall --unused -y" ;;
      flatpak-orphans)
        if [[ -n "$orphans_file" && -f "$orphans_file" ]]; then
          while IFS= read -r oid || [[ -n "$oid" ]]; do
            [[ -z "$oid" ]] && continue
            _queue_user "rm -rf \"$HOME/.var/app/$oid\""
          done < "$orphans_file"
        fi
        ;;
      flathub) _queue_user "flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo || flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo" ;;
      rpmfusion) _queue_priv "rpmfusion_enable" ;;
      codecs) _queue_priv "codecs_install" ;;
      zram) _queue_priv "zram_enable" ;;
      earlyoom) _queue_priv "earlyoom_enable" ;;
      power-ppd) _queue_priv "ppd_enable" ;;
      power-balanced) _queue_user "$(_ppd_set_cmd balanced)" ;;
      power-performance) _queue_user "$(_ppd_set_cmd performance)" ;;
      power-saver) _queue_user "$(_ppd_set_cmd power-saver)" ;;
      tlp-conflict) _queue_priv "tlp_disable" ;;
      fstrim) _queue_priv "fstrim_all" ;;
      dnf-speed) _queue_priv "dnf_speed_conf" ;;
      sysctl) _queue_priv "sysctl_urstack" ;;
      userunit-*)
        local unit="${id#userunit-}"
        _queue_user "systemctl --user disable --now '$unit'"
        ;;
      *)
        _health_prog "Skipping unknown id: $id"
        ;;
    esac
  done

  local ec=0
  local -a applied_ids=()
  if [[ -s "$jobs" ]]; then
    _health_prog "Running privileged steps (polkit)…"
    echo "25"
    if ! _health_run_priv_jobs "$jobs"; then
      ec=1
      _health_prog "Privileged steps reported errors"
    else
      _health_prog "Privileged steps finished"
    fi
  fi
  rm -f "$jobs"
  echo "55"

  local cmd
  for cmd in "${user_cmds[@]}"; do
    step=$((step + 1))
    _health_prog "Running: $cmd"
    if bash -lc "$cmd"; then
      _health_prog "OK"
    else
      _health_prog "FAILED: $cmd"
      ec=1
    fi
    local n_user=${#user_cmds[@]}
    [[ "$n_user" -lt 1 ]] && n_user=1
    echo $((55 + step * 40 / n_user))
  done

  # Remember successful one-shots so the next scan stops nagging
  if [[ "$ec" -eq 0 ]]; then
    for id in "${!want[@]}"; do
      case "$id" in
        fstrim|dnf-cache|journal-vacuum|sysctl|dnf-speed|flatpak-unused|flatpak-orphans|userunit-*|trash|thumbnails|pip-cache|npm-cache|cargo-cache|dnf-autoremove|podman-prune|docker-prune|coredumps|snap-old)
          _health_mark_applied "$id"
          applied_ids+=("$id")
          ;;
      esac
    done
  else
    # Still stamp ones that are clearly one-shot / user-local even on partial failure
    for id in "${!want[@]}"; do
      case "$id" in
        fstrim|dnf-cache)
          _health_mark_applied "$id"
          ;;
      esac
    done
  fi

  echo "100"
  _health_prog "Health apply complete"
  return "$ec"
}
