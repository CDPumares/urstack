# Workstation scanner — detect installed tools and build config.conf
# shellcheck shell=bash

# Sets DETECT_* variables (0/1) and DETECT_NOTES associative-like lines in DETECT_REASON_*

_detect_cmd() { command -v "$1" &>/dev/null; }

_detect_has_appimages() {
  local d
  for d in "$HOME/Applications" "$HOME/AppImages" "$HOME/bin"; do
    [[ -d "$d" ]] || continue
    find "$d" -maxdepth 2 -type f -name '*.AppImage' -print -quit 2>/dev/null | grep -q .
    return $?
  done
  return 1
}

_detect_has_npm_user() {
  local prefix="$HOME/.local/lib/node_modules"
  [[ -d "$prefix" ]] || return 1
  # any package dir besides empty
  find "$prefix" -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null | grep -q .
}

_detect_has_pip_user() {
  # User-site packages often live under ~/.local/lib/python*/site-packages
  find "$HOME/.local/lib" -maxdepth 3 -type d -name site-packages -print -quit 2>/dev/null | grep -q .
}

_detect_has_toolbox_or_distrobox() {
  if _detect_cmd toolbox && _detect_cmd podman; then
    return 0
  fi
  if _detect_cmd distrobox; then
    return 0
  fi
  return 1
}

_detect_has_jetbrains() {
  [[ -x "$HOME/.local/share/JetBrains/Toolbox/bin/jetbrains-toolbox" ]] && return 0
  [[ -x "$HOME/.local/bin/jetbrains-toolbox" ]] && return 0
  _detect_cmd jetbrains-toolbox && return 0
  [[ -d "$HOME/.local/share/JetBrains/Toolbox" ]] && return 0
  return 1
}

_detect_plasma_discover_installed() {
  rpm -q plasma-discover &>/dev/null || rpm -qa 'plasma-discover*' 2>/dev/null | grep -q .
}

# Populate DETECT_enable_* and DETECT_reason_* globals
scan_workstation() {
  DETECT_enable_dnf=0
  DETECT_enable_flatpak=0
  DETECT_enable_snap=0
  DETECT_enable_fw=0
  DETECT_enable_kernel_prune=0
  DETECT_exclude_discover=0
  DETECT_enable_toolbox=0
  DETECT_enable_npm=0
  DETECT_enable_npm_user=0
  DETECT_enable_pip=0
  DETECT_enable_pipx=0
  DETECT_enable_rust=0
  DETECT_enable_cargo=0
  DETECT_enable_node=0
  DETECT_enable_cursor=0
  DETECT_enable_claude=0
  DETECT_enable_supabase=0
  DETECT_enable_jetbrains=0
  DETECT_enable_appimage=0
  DETECT_enable_backup=0
  DETECT_keep_kernels=3
  DETECT_quiet_gnome_software=1

  DETECT_reason_dnf=""
  DETECT_reason_flatpak=""
  DETECT_reason_snap=""
  DETECT_reason_fw=""
  DETECT_reason_toolbox=""
  DETECT_reason_npm=""
  DETECT_reason_npm_user=""
  DETECT_reason_pip=""
  DETECT_reason_pipx=""
  DETECT_reason_rust=""
  DETECT_reason_cargo=""
  DETECT_reason_node=""
  DETECT_reason_cursor=""
  DETECT_reason_claude=""
  DETECT_reason_supabase=""
  DETECT_reason_jetbrains=""
  DETECT_reason_appimage=""
  DETECT_reason_exclude_discover=""

  if _detect_cmd dnf; then
    DETECT_enable_dnf=1
    DETECT_enable_kernel_prune=1
    DETECT_reason_dnf="dnf found ($(dnf --version 2>/dev/null | head -1 | tr -d '\n' || echo present))"
  fi
  if _detect_cmd flatpak; then
    DETECT_enable_flatpak=1
    DETECT_reason_flatpak="flatpak found"
  fi
  if _detect_cmd snap; then
    DETECT_enable_snap=1
    DETECT_reason_snap="snap found"
  fi
  if _detect_cmd fwupdmgr; then
    DETECT_enable_fw=1
    DETECT_reason_fw="fwupdmgr found"
  fi

  # If Discover is installed, don't force-exclude it; if absent, exclude so DNF won't pull it back
  if _detect_plasma_discover_installed; then
    DETECT_exclude_discover=0
    DETECT_reason_exclude_discover="plasma-discover is installed — leave exclude off"
  else
    DETECT_exclude_discover=1
    DETECT_reason_exclude_discover="plasma-discover not installed — exclude so upgrades don't reinstall it"
  fi

  if _detect_has_toolbox_or_distrobox; then
    DETECT_enable_toolbox=1
    DETECT_reason_toolbox="toolbox and/or distrobox present"
  fi

  if _detect_cmd npm; then
    DETECT_enable_npm=1
    DETECT_reason_npm="npm found ($(npm --version 2>/dev/null | head -1))"
  fi
  if _detect_has_npm_user; then
    DETECT_enable_npm_user=1
    # DETECT_reason_* is only ever printed, so the tilde is deliberate prose.
    # shellcheck disable=SC2088
    DETECT_reason_npm_user="~/.local/lib/node_modules has packages"
  fi

  if _detect_cmd python3 && python3 -m pip --version &>/dev/null; then
    if _detect_has_pip_user; then
      DETECT_enable_pip=1
      DETECT_reason_pip="user site-packages found under ~/.local/lib"
    fi
  fi
  if _detect_cmd pipx; then
    DETECT_enable_pipx=1
    DETECT_reason_pipx="pipx found"
  fi

  if _detect_cmd rustup; then
    DETECT_enable_rust=1
    DETECT_reason_rust="rustup found"
  fi
  if _detect_cmd cargo; then
    DETECT_enable_cargo=1
    DETECT_reason_cargo="cargo found"
  fi

  if [[ -f "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]]; then
    DETECT_enable_node=1
    DETECT_reason_node="nvm found at ${NVM_DIR:-$HOME/.nvm}"
  fi

  if rpm -q cursor &>/dev/null || _detect_cmd cursor; then
    DETECT_enable_cursor=1
    DETECT_reason_cursor="Cursor installed"
  fi
  if _detect_cmd claude; then
    DETECT_enable_claude=1
    DETECT_reason_claude="claude CLI found"
  fi
  if _detect_cmd supabase; then
    DETECT_enable_supabase=1
    DETECT_reason_supabase="supabase CLI found"
  fi

  if _detect_has_jetbrains; then
    DETECT_enable_jetbrains=1
    DETECT_reason_jetbrains="JetBrains Toolbox present"
  fi
  if _detect_has_appimages; then
    DETECT_enable_appimage=1
    DETECT_reason_appimage="AppImage(s) under ~/Applications, ~/AppImages, or ~/bin"
  fi

  # Backup stays off unless explicitly requested — heavy / personal
  DETECT_enable_backup=0

  # Quiet GNOME Software if present as a user service candidate
  if systemctl --user list-unit-files gnome-software.service &>/dev/null \
      || rpm -q gnome-software &>/dev/null; then
    DETECT_quiet_gnome_software=1
  else
    DETECT_quiet_gnome_software=0
  fi
}

print_detection_report() {
  scan_workstation
  echo "Workstation scan results"
  echo "========================"
  local key label val reason
  for key in dnf flatpak snap fw toolbox npm npm_user pip pipx rust cargo node cursor claude supabase jetbrains appimage; do
    eval "val=\${DETECT_enable_${key}:-0}"
    eval "reason=\${DETECT_reason_${key}:-}"
    if [[ "$val" == "1" ]]; then
      printf '  [ON ] %-12s %s\n' "$key" "${reason:+— $reason}"
    else
      printf '  [off] %-12s\n' "$key"
    fi
  done
  echo
  printf '  exclude_discover=%s  %s\n' "$DETECT_exclude_discover" "${DETECT_reason_exclude_discover:+— $DETECT_reason_exclude_discover}"
  printf '  kernel_prune=%s  keep_kernels=%s\n' "$DETECT_enable_kernel_prune" "$DETECT_keep_kernels"
  printf '  backup=%s (not auto-enabled; pass --include-backup to turn on)\n' "$DETECT_enable_backup"
}

# Write config to path (stdout if path is -)
write_detected_config() {
  local out="${1:-}"
  local include_backup="${2:-0}"
  scan_workstation
  if [[ "$include_backup" == "1" ]]; then
    DETECT_enable_backup=1
  fi

  local prev_appearance="system"
  local prev_apply_fw="0"
  if [[ -n "$out" && "$out" != "-" && -f "$out" ]]; then
    prev_appearance="$(awk -F= '/^appearance=/{gsub(/[[:space:]]/, "", $2); print $2; exit}' "$out")"
    [[ "$prev_appearance" == "light" || "$prev_appearance" == "dark" || "$prev_appearance" == "system" ]] \
      || prev_appearance=system
    prev_apply_fw="$(awk -F= '/^apply_fw=/{
      val=$2
      sub(/#.*/, "", val)
      gsub(/[[:space:]]/, "", val)
      print val
      exit
    }' "$out")"
    [[ "$prev_apply_fw" == "1" ]] || prev_apply_fw=0
  fi

  local content
  content=$(cat <<EOF
# UrStack — generated by workstation scan
# $(date -Iseconds)
# Host: $(hostname -f 2>/dev/null || hostname)
# Re-scan anytime: stackup --detect --write-config
#
# Edit freely; 1=on, 0=off

# ── Core ─────────────────────────────────────────────────────────────────────
enable_dnf=${DETECT_enable_dnf}          # ${DETECT_reason_dnf:-not found}
enable_flatpak=${DETECT_enable_flatpak}     # ${DETECT_reason_flatpak:-not found}
enable_snap=${DETECT_enable_snap}        # ${DETECT_reason_snap:-not found}
enable_fw=${DETECT_enable_fw}          # ${DETECT_reason_fw:-not found}
apply_fw=${prev_apply_fw}            # install fwupd payloads (opt-in; reboot)
enable_kernel_prune=${DETECT_enable_kernel_prune}
exclude_discover=${DETECT_exclude_discover}   # ${DETECT_reason_exclude_discover}

# ── Plugins (detected on this machine) ───────────────────────────────────────
enable_toolbox=${DETECT_enable_toolbox}     # ${DETECT_reason_toolbox:-not found}
enable_npm=${DETECT_enable_npm}         # ${DETECT_reason_npm:-not found}
enable_npm_user=${DETECT_enable_npm_user}    # ${DETECT_reason_npm_user:-not found}
enable_pip=${DETECT_enable_pip}         # ${DETECT_reason_pip:-not found}
enable_pipx=${DETECT_enable_pipx}        # ${DETECT_reason_pipx:-not found}
enable_rust=${DETECT_enable_rust}        # ${DETECT_reason_rust:-not found}
enable_cargo=${DETECT_enable_cargo}       # ${DETECT_reason_cargo:-not found}
enable_node=${DETECT_enable_node}        # ${DETECT_reason_node:-not found}
enable_cursor=${DETECT_enable_cursor}      # ${DETECT_reason_cursor:-not found}
enable_claude=${DETECT_enable_claude}      # ${DETECT_reason_claude:-not found}
enable_supabase=${DETECT_enable_supabase}    # ${DETECT_reason_supabase:-not found}
enable_jetbrains=${DETECT_enable_jetbrains}   # ${DETECT_reason_jetbrains:-not found}
enable_appimage=${DETECT_enable_appimage}    # ${DETECT_reason_appimage:-not found}

# Backup / restore (large; opt-in)
enable_backup=${DETECT_enable_backup}
backup_project_roots=Documents:Projects:src:Desktop:waydroid_script
backup_project_depth=3
backup_full_dotconfig=1

# ── Behaviour ────────────────────────────────────────────────────────────────
keep_kernels=${DETECT_keep_kernels}
quiet_gnome_software=${DETECT_quiet_gnome_software}
appearance=${prev_appearance}
EOF
)

  if [[ -z "$out" || "$out" == "-" ]]; then
    printf '%s\n' "$content"
    return 0
  fi
  mkdir -p "$(dirname "$out")"
  # Backup existing config once
  if [[ -f "$out" ]]; then
    cp -a "$out" "${out}.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  printf '%s\n' "$content" > "$out"
  echo "Wrote $out"
}
