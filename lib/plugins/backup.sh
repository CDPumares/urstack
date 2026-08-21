#!/usr/bin/env bash
# Fedora workstation backup / restore library.
# Sourced by bin/urstack — do not execute directly.
#
# Exports: fedora_setup_backup_ui, fedora_setup_restore_ui,
#          fedora_setup_backup_to, fedora_setup_restore_from

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
_FEDORA_SETUP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# plugins/ → app root is ../..
_FEDORA_SETUP_ROOT="$(cd "$_FEDORA_SETUP_LIB_DIR/../.." && pwd)"
_FEDORA_SETUP_BIN_DIR="$_FEDORA_SETUP_ROOT/bin"
_FEDORA_SETUP_SCRIPT="${_FEDORA_SETUP_BIN_DIR}/urstack"
_FEDORA_UI="${FEDORA_UPDATES_LIB:-$_FEDORA_SETUP_ROOT/lib/core}/ui.py"

_RSYNC_PROJECT_EXCLUDES=(
  --exclude=node_modules/
  --exclude=.next/
  --exclude=dist/
  --exclude=build/
  --exclude=target/
  --exclude=.venv/
  --exclude=venv/
  --exclude=__pycache__/
  --exclude=.turbo/
  --exclude=coverage/
  --exclude='*.AppImage'
)

# Default project roots for backup (overridable via backup_project_roots in config.conf).
# Paths may be absolute or relative to $HOME. Colon- or comma-separated.
_DEFAULT_PROJECT_ROOTS=(
  "Documents"
  "Projects"
  "src"
  "Desktop"
  "waydroid_script"
)

# ---------------------------------------------------------------------------
# Config helpers (works after load_updater_config / with cfg_get)
# ---------------------------------------------------------------------------
_backup_cfg() {
  # Prefer cfg_get when available; else read user config directly
  local key="$1" default="${2:-}"
  if declare -F cfg_get &>/dev/null; then
    cfg_get "$key" "$default"
    return
  fi
  local f="${FEDORA_UPDATES_USER_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/urstack/config.conf}"
  local val=""
  if [[ -f "$f" ]]; then
    val=$(grep -E "^[[:space:]]*${key}=" "$f" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/^[[:space:]]*//;s/[[:space:]]*$//') || true
  fi
  printf '%s' "${val:-$default}"
}

_backup_project_roots() {
  # Prints absolute directory paths (one per line) that exist
  local raw depth_unused
  raw="$(_backup_cfg backup_project_roots "")"
  local -a roots=()
  local item
  if [[ -n "$raw" ]]; then
    IFS=':,' read -ra roots <<< "$raw"
  else
    roots=("${_DEFAULT_PROJECT_ROOTS[@]}")
  fi
  for item in "${roots[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ -n "$item" ]] || continue
    if [[ "$item" != /* ]]; then
      item="$HOME/$item"
    fi
    [[ -d "$item" ]] && printf '%s\n' "$item"
  done
}

_backup_project_depth() {
  local d
  d="$(_backup_cfg backup_project_depth 3)"
  [[ "$d" =~ ^[0-9]+$ ]] || d=3
  [[ "$d" -lt 1 ]] && d=1
  [[ "$d" -gt 6 ]] && d=6
  printf '%s' "$d"
}

# Per-run include flags from the UI (URSTACK_BACKUP_OPTS="key=1,key=0,...").
# Empty opts → use defaults (full_dotconfig follows config.conf).
_backup_include() {
  local key="$1"
  local default="${2:-1}"
  local opts="${URSTACK_BACKUP_OPTS:-}"
  if [[ -z "$opts" ]]; then
    if [[ "$key" == "full_dotconfig" ]]; then
      default="$(_backup_cfg backup_full_dotconfig 1)"
    fi
    printf '%s' "$default"
    return
  fi
  local part
  local IFS=','
  # shellcheck disable=SC2086
  for part in $opts; do
    case "$part" in
      "$key=0"|"$key=false"|"$key=no") printf '0'; return ;;
      "$key=1"|"$key=true"|"$key=yes"|"$key") printf '1'; return ;;
    esac
  done
  if [[ "$key" == "full_dotconfig" ]]; then
    default="$(_backup_cfg backup_full_dotconfig 1)"
  fi
  printf '%s' "$default"
}

# Restore step tracking (files survive pipeline subshells)
_RESTORE_OK_FILE=""
_RESTORE_FAIL_FILE=""
_RESTORE_SKIP_FILE=""

_restore_track_init() {
  _RESTORE_OK_FILE=$(mktemp)
  _RESTORE_FAIL_FILE=$(mktemp)
  _RESTORE_SKIP_FILE=$(mktemp)
}

_restore_track_cleanup() {
  rm -f "${_RESTORE_OK_FILE:-}" "${_RESTORE_FAIL_FILE:-}" "${_RESTORE_SKIP_FILE:-}"
  _RESTORE_OK_FILE=""
  _RESTORE_FAIL_FILE=""
  _RESTORE_SKIP_FILE=""
}

_restore_ok() { echo "$1" >> "${_RESTORE_OK_FILE:-/dev/null}"; }
_restore_fail() { echo "$1" >> "${_RESTORE_FAIL_FILE:-/dev/null}"; }
_restore_skip() { echo "$1" >> "${_RESTORE_SKIP_FILE:-/dev/null}"; }

# Run a restore step; on failure record label (does not abort the restore).
_restore_try() {
  local label="$1"
  shift
  if "$@"; then
    _restore_ok "$label"
    return 0
  fi
  _restore_fail "$label"
  echo "# FAILED: $label" >&2
  return 1
}

# Restoring into $HOME is otherwise irreversible: rsync -a replaces any file whose
# size or mtime differs, so an older blueprint silently reverts current work in
# project trees and dotfiles. Every replaced file is moved here first.
_PRE_RESTORE_DIR=""
declare -a _HOME_RSYNC_OPTS=()

_ensure_pre_restore_dir() {
  [[ ${#_HOME_RSYNC_OPTS[@]} -gt 0 ]] && return 0
  local d
  d="${XDG_STATE_HOME:-$HOME/.local/state}/urstack/pre-restore-$(date +%Y%m%d-%H%M%S)"
  if mkdir -p "$d" 2>/dev/null; then
    _PRE_RESTORE_DIR="$d"
    _HOME_RSYNC_OPTS=(--backup --backup-dir="$d")
  else
    echo "# WARNING: could not create $d — restore will not be reversible" >&2
  fi
  return 0
}

_b64() { printf '%s' "$1" | base64 -w0; }

# Package names out of a backup are handed to dnf as root, where anything that
# could pass as a flag (--installroot=, --nogpgcheck), a glob, or a path to a
# local .rpm with a %post scriptlet would be honoured. Only plain names pass.
_valid_pkg_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]
}

# Run privileged restore jobs through the hardened helper. Nothing the caller
# controls reaches a root shell: priv.sh reads a jobs file, re-validates the
# decoded path's ownership, and maps the destination from a fixed allowlist.
# Multiple jobs share one pkexec prompt; #env lines wire cancel/undo paths.
_RESTORE_CANCEL=""
_RESTORE_UNDO=""

_restore_init_cancel_undo() {
  local state="${XDG_STATE_HOME:-$HOME/.local/state}/urstack"
  mkdir -p "$state" || return 1
  _RESTORE_UNDO=$(mktemp -d "$state/restore-undo.XXXXXX") || return 1
  chmod 700 "$_RESTORE_UNDO"
  _RESTORE_CANCEL="$_RESTORE_UNDO/cancel"
}

_restore_run_priv_batch() {
  local jobs ec
  jobs=$(mktemp) || return 1
  chmod 600 "$jobs"
  {
    [[ -n "${_RESTORE_CANCEL:-}" ]] && printf '#env URSTACK_RESTORE_CANCEL=%s\n' "$_RESTORE_CANCEL"
    [[ -n "${_RESTORE_UNDO:-}" ]] && printf '#env URSTACK_RESTORE_UNDO=%s\n' "$_RESTORE_UNDO"
    printf '%s\n' "$@"
  } > "$jobs"
  pkexec_priv "$jobs" 2>&1
  ec=$?
  rm -f "$jobs"
  return "$ec"
}

_priv_restore() {
  _restore_run_priv_batch "$*"
}

_restore_unwind_userspace() {
  if [[ -n "${_PRE_RESTORE_DIR:-}" && -d "$_PRE_RESTORE_DIR" ]]; then
    echo "# Previous home files were copied aside under $_PRE_RESTORE_DIR" >&2
  fi
  _restore_run_priv_batch restore_session_unwind || true
}

# ── Blueprint integrity ──────────────────────────────────────────────────────
# Restoring a blueprint installs packages as root and writes into /etc, so the
# tree is security-relevant input. A checksum manifest lets a restore notice the
# blueprint changed after it was written. This proves integrity, not provenance:
# it cannot tell you a blueprint came from someone you trust, only that it is
# byte-for-byte what was recorded.
_MANIFEST_NAME="MANIFEST.sha256"

# Derived, human-readable artefacts only. BACKUP_SUMMARY.txt and
# BACKUP_MANIFEST.md are rebuilt whenever the summary dialog is shown, and
# RESTORE_REPORT.txt is written into the blueprint during a restore, so none of
# them is stable enough to checksum. Nothing here is an input to a restore.
# The incomplete marker is a status flag rather than blueprint content, and it
# is removed after the manifest is built, so checksumming it would guarantee a
# mismatch on every restore.
_manifest_find() {
  find . -type f \
    ! -path "./$_MANIFEST_NAME" \
    ! -path "./$URSTACK_INCOMPLETE_NAME" \
    ! -path "./BACKUP_SUMMARY.txt" \
    ! -path "./BACKUP_MANIFEST.md" \
    ! -path "./RESTORE_REPORT.txt" \
    ! -path "./RESTORE_LOG.txt" "$@"
}

_write_backup_manifest() {
  local dest="$1" tmp
  tmp=$(mktemp) || return 1
  # Built outside the tree: a temp file inside it would race with its own find.
  if ! ( cd "$dest" && _manifest_find -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum ) \
        > "$tmp" 2>/dev/null; then
    rm -f "$tmp"; return 1
  fi
  install -m 600 "$tmp" "$dest/$_MANIFEST_NAME" || { rm -f "$tmp"; return 1; }
  rm -f "$tmp"
}

# 0 = intact, 1 = problems written to $2, 2 = blueprint predates the manifest.
_verify_backup_manifest() {
  local dest="$1" report="$2"
  [[ -f "$dest/$_MANIFEST_NAME" ]] || return 2
  : > "$report"
  local rc=0 extras x raw
  raw=$(mktemp) || return 1
  ( cd "$dest" && sha256sum -c --quiet "$_MANIFEST_NAME" ) > "$raw" 2>&1 || rc=1
  # Keep only the per-file verdicts; sha256sum's own summary lines would
  # otherwise be counted as if they were tampered files.
  grep -v '^sha256sum:' "$raw" >> "$report" || true
  rm -f "$raw"
  # A file that was added after the manifest was written is equally suspicious.
  # comm must collate the same way its inputs were sorted, or it silently
  # reports every line as unique — which here would mean "the whole blueprint
  # is unlisted". Uppercase names like RESTORE_LOG.txt are enough to trip it.
  extras=$(LC_ALL=C comm -13 \
    <(sed -n 's/^[0-9a-f]\{64\}  //p' "$dest/$_MANIFEST_NAME" | LC_ALL=C sort) \
    <(cd "$dest" && _manifest_find -print | LC_ALL=C sort))
  while IFS= read -r x; do
    [[ -n "$x" ]] || continue
    printf '%s: not listed in the manifest\n' "$x" >> "$report"
    rc=1
  done <<< "$extras"
  return $rc
}

# Everything a restore feeds to root: package lists, repo files, /etc content.
_manifest_path_is_privileged() {
  case "$1" in
    ./manifests/*|./config/etc/*) return 0 ;;
  esac
  return 1
}

# rsync -a preserves modes from the backup medium. A blueprint written to exFAT
# or NTFS comes back as 0777, which both leaks private keys to every local user
# and makes OpenSSH and GnuPG refuse to use them. Reassert the modes ourselves.
_repair_secret_modes() {
  local f
  if [[ -d "$HOME/.ssh" ]]; then
    chmod 700 "$HOME/.ssh" 2>/dev/null || _restore_fail "Securing ~/.ssh"
    find "$HOME/.ssh" -type f ! -name '*.pub' ! -name 'known_hosts*' \
      -exec chmod 600 {} + 2>/dev/null || _restore_fail "Securing ~/.ssh keys"
    find "$HOME/.ssh" -type f \( -name '*.pub' -o -name 'known_hosts*' \) \
      -exec chmod 644 {} + 2>/dev/null || true
  fi
  if [[ -d "$HOME/.gnupg" ]]; then
    find "$HOME/.gnupg" -type d -exec chmod 700 {} + 2>/dev/null || _restore_fail "Securing ~/.gnupg"
    find "$HOME/.gnupg" -type f -exec chmod 600 {} + 2>/dev/null || _restore_fail "Securing ~/.gnupg"
  fi
  for f in "$HOME/.netrc" "$HOME/.git-credentials"; do
    [[ -f "$f" ]] && { chmod 600 "$f" 2>/dev/null || _restore_fail "Securing $(basename "$f")"; }
  done
  return 0
}

_restore_write_report() {
  local dest="$1" logf="${2:-}"
  local report="$dest/RESTORE_REPORT.txt"
  local okc failc skipc
  okc=$(wc -l < "${_RESTORE_OK_FILE:-/dev/null}" 2>/dev/null | tr -d ' ')
  failc=$(wc -l < "${_RESTORE_FAIL_FILE:-/dev/null}" 2>/dev/null | tr -d ' ')
  skipc=$(wc -l < "${_RESTORE_SKIP_FILE:-/dev/null}" 2>/dev/null | tr -d ' ')
  {
    echo "UrStack restore report"
    echo "Date: $(date -Iseconds)"
    echo "Backup: $dest"
    echo "OK: ${okc:-0}  Failed: ${failc:-0}  Skipped: ${skipc:-0}"
    echo ""
    if [[ -n "$_PRE_RESTORE_DIR" && -d "$_PRE_RESTORE_DIR" ]]; then
      echo "== Undo =="
      echo "Files in \$HOME that this restore replaced were saved to:"
      echo "  $_PRE_RESTORE_DIR"
      echo "Copy anything back from there if the restore overwrote newer work."
      echo ""
    fi
    if [[ -s "${_RESTORE_FAIL_FILE:-}" ]]; then
      echo "== Failed steps =="
      sed 's/^/- /' "${_RESTORE_FAIL_FILE}"
      echo ""
    fi
    if [[ -s "${_RESTORE_SKIP_FILE:-}" ]]; then
      echo "== Skipped =="
      sed 's/^/- /' "${_RESTORE_SKIP_FILE}"
      echo ""
    fi
    if [[ -s "${_RESTORE_OK_FILE:-}" ]]; then
      echo "== Completed =="
      sed 's/^/- /' "${_RESTORE_OK_FILE}"
      echo ""
    fi
    echo "Next steps:"
    echo "- Log out / restart desktop session for layout themes"
    echo "- Rejoin Wi-Fi / VPN; sign into browsers and Flatpaks"
    echo "- Run npm/pnpm install inside project trees if needed"
    echo "- AppImages were restored under ~/Applications when present"
    echo "- Review manifests/vendor-launchers.txt for apps that need a vendor installer"
    if [[ -n "$logf" && -f "$logf" ]]; then
      echo ""
      echo "Full log: $logf"
    fi
  } > "$report"
  printf '%s' "$report"
}

# NVIDIA PCI vendor, AMD, Intel
_PCI_NVIDIA=10de
_PCI_AMD=1002
_PCI_INTEL=8086

# ---------------------------------------------------------------------------
# Small helpers (GTK UI when available; zenity / terminal fallback)
# ---------------------------------------------------------------------------
_FEDORA_UI="${FEDORA_UPDATES_LIB:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../core" && pwd)}/ui.py"

_fs_ui() {
  # Run ui.py; returns its exit status. Stdout is the dialog result.
  [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -f "$_FEDORA_UI" ]] || return 127
  python3 "$_FEDORA_UI" "$@" 2>/dev/null
}

_fs_msg() {
  if _fs_ui message --type info --title "Fedora Setup" --text "$1"; then
    return 0
  fi
  if command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    zenity --info --title="Fedora Setup" --text="$1" --width=480 2>/dev/null || echo "$1"
  else
    echo "$1"
  fi
}

_fs_err() {
  if _fs_ui message --type error --title "Fedora Setup" --text "$1"; then
    return 0
  fi
  if command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    zenity --error --title="Fedora Setup" --text="$1" --width=480 2>/dev/null || echo "ERROR: $1" >&2
  else
    echo "ERROR: $1" >&2
  fi
}

_fs_ask() {
  if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]]; then
    _fs_ui ask --title "Fedora Setup" --text "$1"
    return $?
  fi
  if command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    zenity --question --title="Fedora Setup" --text="$1" --width=480 2>/dev/null
  else
    local r
    read -rp "$1 [y/N] " r
    [[ "${r:-}" =~ ^[yY] ]]
  fi
}

_fs_progress() {
  # usage: _fs_progress "label"  — echoes progress line for zenity --progress
  echo "# $1"
}

_copy_file() {
  local src="$1" dest="$2"
  [[ -e "$src" || -L "$src" ]] || return 0
  mkdir -p "$(dirname "$dest")"
  cp -a "$src" "$dest" 2>/dev/null || true
}

_copy_tree() {
  local src="$1" dest="$2"
  [[ -e "$src" ]] || return 0
  mkdir -p "$(dirname "$dest")"
  cp -a "$src" "$dest" 2>/dev/null || true
}

# Copy path relative to $HOME into home-overlay
_overlay_home() {
  local rel="${1#"$HOME"/}"
  local src="$HOME/$rel"
  local dest="$2/config/home-overlay/$rel"
  [[ -e "$src" || -L "$src" ]] || return 0
  if [[ -d "$src" && ! -L "$src" ]]; then
    mkdir -p "$dest"
    rsync -a --delete "${@:3}" "$src"/ "$dest"/ 2>/dev/null || cp -a "$src"/. "$dest"/ 2>/dev/null || true
  else
    _copy_file "$src" "$dest"
  fi
}

_overlay_etc() {
  local rel="$1" # e.g. default/grub or cups/printers.conf
  local src="/etc/$rel"
  local dest="$2/config/etc/$rel"
  [[ -e "$src" ]] || return 0
  if [[ -d "$src" ]]; then
    mkdir -p "$dest"
    rsync -a "$src"/ "$dest"/ 2>/dev/null || cp -a "$src"/. "$dest"/ 2>/dev/null || true
  else
    _copy_file "$src" "$dest"
  fi
}

_pci_has_vendor() {
  local vend="$1"
  lspci -nn 2>/dev/null | grep -qi "\[${vend}:"
}

_detect_gpus() {
  # sets HAS_NVIDIA HAS_AMD HAS_INTEL_GPU
  HAS_NVIDIA=0 HAS_AMD=0 HAS_INTEL_GPU=0
  local line
  while IFS= read -r line; do
    echo "$line" | grep -qiE 'VGA|3D|Display' || continue
    echo "$line" | grep -qi "\[${_PCI_NVIDIA}:" && HAS_NVIDIA=1
    echo "$line" | grep -qi "\[${_PCI_AMD}:" && HAS_AMD=1
    echo "$line" | grep -qi "\[${_PCI_INTEL}:" && HAS_INTEL_GPU=1
  done < <(lspci -nn 2>/dev/null)
}

_dated_backup_dir() {
  local parent="$1"
  local day stamp
  day="$(date +%Y-%m-%d)"
  stamp="$parent/fedora-setup-$day"
  if [[ -e "$stamp" ]]; then
    stamp="$parent/fedora-setup-$day-$(date +%H%M)"
  fi
  echo "$stamp"
}

_pick_directory() {
  local title="$1" start="${2:-$HOME}" picked=""
  if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]]; then
    picked=$(_fs_ui folder --title "$title" --start "$start") || true
    [[ -n "$picked" ]] && { echo "$picked"; return 0; }
    return 1
  fi
  if command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    zenity --file-selection --directory --title="$title" --filename="$start/" 2>/dev/null
  else
    local d
    read -rp "$title [$start]: " d
    echo "${d:-$start}"
  fi
}

# ---------------------------------------------------------------------------
# Driver recipes (backup + restore)
# ---------------------------------------------------------------------------
_write_hw_inventory() {
  local out="$1"
  {
    echo "=== lspci -nn ==="
    lspci -nn 2>/dev/null || true
    echo
    echo "=== lsusb ==="
    lsusb 2>/dev/null || true
    echo
    echo "=== modules (gpu/net/audio interest) ==="
    lsmod 2>/dev/null | grep -iE 'nvidia|amdgpu|i915|iwlwifi|xpad|snd_' || true
  } > "$out"
}

_write_drivers_manifest() {
  local dest="$1"
  local txt="$dest/manifests/drivers.txt"
  local json="$dest/manifests/drivers.json"
  _detect_gpus

  {
    echo "# Driver groups captured on $(hostname) at $(date -Iseconds)"
    echo "HAS_NVIDIA=$HAS_NVIDIA"
    echo "HAS_AMD=$HAS_AMD"
    echo "HAS_INTEL_GPU=$HAS_INTEL_GPU"
    echo
    if [[ $HAS_NVIDIA -eq 1 ]]; then
      echo "## nvidia"
      rpm -qa 'akmod-nvidia*' 'xorg-x11-drv-nvidia*' 'libva-nvidia*' 'nvidia-*' 2>/dev/null | sort || true
    fi
    if [[ $HAS_INTEL_GPU -eq 1 ]]; then
      echo "## intel-gpu"
      rpm -qa 'libva-intel*' 'intel-media*' 'intel-gpu*' 2>/dev/null | sort || true
    fi
    if [[ $HAS_AMD -eq 1 ]]; then
      echo "## amd-gpu"
      rpm -qa 'mesa-vulkan*' 'amd-gpu*' 2>/dev/null | sort || true
    fi
    echo "## always"
    rpm -qa 'input-remapper*' 'linux-firmware' 2>/dev/null | sort || true
    echo "## xpadneo-repo"
    [[ -f /etc/yum.repos.d/_copr:copr.fedorainfracloud.org:atim:xpadneo.repo ]] && echo "repo:atim/xpadneo"
  } > "$txt"

  # JSON for restore matching
  python3 - "$json" "$HAS_NVIDIA" "$HAS_AMD" "$HAS_INTEL_GPU" <<'PY'
import json, sys
path, has_n, has_a, has_i = sys.argv[1], sys.argv[2]=="1", sys.argv[3]=="1", sys.argv[4]=="1"
groups = []
if has_n:
    groups.append({
        "id": "nvidia",
        "need": {"pci_vendor": "10de"},
        "packages": ["akmod-nvidia", "xorg-x11-drv-nvidia", "xorg-x11-drv-nvidia-cuda",
                     "libva-nvidia-driver", "nvidia-settings", "nvidia-modprobe"],
        "repos": ["rpmfusion-nonfree-nvidia-driver"],
        "grub_nvidia": True,
    })
if has_i:
    groups.append({
        "id": "intel-gpu",
        "need": {"pci_vendor": "8086", "class_re": "VGA|3D|Display"},
        "packages": ["libva-intel-media-driver"],
    })
if has_a:
    groups.append({
        "id": "amd-gpu",
        "need": {"pci_vendor": "1002", "class_re": "VGA|3D|Display"},
        "packages": ["mesa-vulkan-drivers"],
    })
groups.append({"id": "input-remapper", "need": "always", "packages": ["input-remapper"]})
groups.append({"id": "linux-firmware", "need": "always", "packages": ["linux-firmware"]})
groups.append({"id": "xpadneo", "need": {"optional_user_confirm": True}, "packages": [], "repos": ["atim/xpadneo"]})
json.dump({"groups": groups, "source_has": {"nvidia": has_n, "amd": has_a, "intel": has_i}}, open(path, "w"), indent=2)
PY
}

_propose_target_driver_groups() {
  # Prints lines: status|id|label|packages_csv
  # status: matched | skipped | new
  local backup_json="$1"
  _detect_gpus
  python3 - "$backup_json" "$HAS_NVIDIA" "$HAS_AMD" "$HAS_INTEL_GPU" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
has_n, has_a, has_i = sys.argv[2]=="1", sys.argv[3]=="1", sys.argv[4]=="1"
target = {"nvidia": has_n, "amd": has_a, "intel": has_i}
seen = set()

def need_ok(need):
    if need == "always":
        return True
    if isinstance(need, dict) and need.get("optional_user_confirm"):
        return False  # off by default on different hw
    if isinstance(need, dict) and "pci_vendor" in need:
        v = need["pci_vendor"]
        if v == "10de": return has_n
        if v == "1002": return has_a
        if v == "8086": return has_i
    return False

for g in data.get("groups", []):
    gid = g["id"]
    seen.add(gid)
    pkgs = ",".join(g.get("packages") or [])
    label = gid
    if need_ok(g.get("need")):
        print(f"matched|{gid}|From backup: {label}|{pkgs}")
    else:
        print(f"skipped|{gid}|Skip (not on this machine): {label}|{pkgs}")

# New for target
recipes = [
    ("nvidia", has_n, "akmod-nvidia,xorg-x11-drv-nvidia,libva-nvidia-driver"),
    ("amd-gpu", has_a, "mesa-vulkan-drivers"),
    ("intel-gpu", has_i, "libva-intel-media-driver"),
]
for gid, present, pkgs in recipes:
    if present and gid not in seen:
        print(f"new|{gid}|New for this machine: {gid}|{pkgs}")
    elif present and gid in seen:
        pass  # already matched from backup
PY
}

# ---------------------------------------------------------------------------
# Backup collectors
# ---------------------------------------------------------------------------
# Write one package name per line from npm (global or --prefix).
# Skips npm itself and non-package noise.
_npm_package_names() {
  local extra_args=("$@")
  python3 - "${extra_args[@]}" <<'PY' 2>/dev/null || true
import json, subprocess, sys
args = ["npm", "ls", "-g", "--depth=0", "--json"] + sys.argv[1:]
try:
    raw = subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)
    data = json.loads(raw or "{}")
except Exception:
    sys.exit(0)
deps = (data or {}).get("dependencies") or {}
skip = {"npm"}
for name in sorted(deps):
    if name in skip:
        continue
    print(name)
PY
}

# Human + machine readable master inventory of programs & CLIs for restore.
_write_programs_inventory() {
  local m="$1"
  local inv="$m/programs-and-clis.txt"
  local json="$m/programs-and-clis.json"

  {
    echo "# Programs & CLIs inventory — $(hostname) @ $(date -Iseconds)"
    echo "# Used by restore to reinstall everything listed below."
    echo

    echo "## DNF (user-installed RPMs)"
    if [[ -f "$m/dnf-user-packages.txt" ]]; then
      local _dnf_n
      _dnf_n=$(wc -l < "$m/dnf-user-packages.txt" 2>/dev/null | tr -d ' ')
      echo "# ${_dnf_n:-0} packages"
      cat "$m/dnf-user-packages.txt" 2>/dev/null || true
    else
      echo "# (none)"
    fi
    echo

    echo "## Flatpak apps"
    if [[ -f "$m/flatpak-apps.txt" ]]; then
      cat "$m/flatpak-apps.txt"
    else
      echo "# (none)"
    fi
    echo

    echo "## Snap packages"
    if [[ -f "$m/snap-packages.txt" ]]; then
      tail -n +2 "$m/snap-packages.txt" 2>/dev/null | awk '{print $1}' || true
    else
      echo "# (none)"
    fi
    echo

    echo "## npm global (nvm) — install: npm i -g \$(cat npm-global-packages.txt)"
    cat "$m/npm-global-packages.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## npm user (~/.local) — install: npm i -g --prefix ~/.local \$(cat npm-user-packages.txt)"
    cat "$m/npm-user-packages.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## pip user — install: pip install --user -r pip-user.txt"
    if [[ -f "$m/pip-user.txt" ]]; then
      local _pip_n
      _pip_n=$(wc -l < "$m/pip-user.txt" 2>/dev/null | tr -d ' ')
      echo "# ${_pip_n:-0} packages"
      cat "$m/pip-user.txt" 2>/dev/null || true
    else
      echo "# (none)"
    fi
    echo

    echo "## pipx — install: pipx install <name>"
    cat "$m/pipx-packages.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## Cargo crates — install: cargo install <crate>"
    cat "$m/cargo-crates.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## Rustup"
    cat "$m/rustup.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## Node / nvm"
    cat "$m/nvm.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## Special CLIs"
    cat "$m/special.json" 2>/dev/null || echo "# (none)"
    echo

    echo "## Cursor extensions — install: cursor --install-extension <id>"
    cat "$m/cursor-extensions.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## ~/.local/bin (CLI entrypoints present on PATH)"
    cat "$m/local-bin.txt" 2>/dev/null || ls -1 "$HOME/.local/bin" 2>/dev/null || echo "# (none)"
    echo

    echo "## ~/.cargo/bin"
    cat "$m/cargo-bin-names.txt" 2>/dev/null || ls -1 "$HOME/.cargo/bin" 2>/dev/null || echo "# (none)"
    echo

    echo "## Notable CLIs on PATH (detected)"
    cat "$m/cli-on-path.txt" 2>/dev/null || echo "# (none)"
    echo

    echo "## User desktop launchers"
    if [[ -f "$m/desktop-files.txt" ]]; then
      cat "$m/desktop-files.txt"
    else
      echo "# (none)"
    fi
  } > "$inv"

  python3 - "$m" "$json" <<'PY'
import json, os, sys
m, out = sys.argv[1], sys.argv[2]

def lines(name):
    path = os.path.join(m, name)
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

def snap_names():
    path = os.path.join(m, "snap-packages.txt")
    if not os.path.isfile(path):
        return []
    names = []
    with open(path) as f:
        for i, ln in enumerate(f):
            if i == 0:
                continue
            parts = ln.split()
            if parts:
                names.append(parts[0])
    return names

def flatpak_apps():
    path = os.path.join(m, "flatpak-apps.txt")
    apps = []
    if not os.path.isfile(path):
        return apps
    with open(path) as f:
        for ln in f:
            parts = ln.split()
            if not parts or parts[0] in ("Application", "application"):
                continue
            origin = parts[1] if len(parts) > 1 else ""
            inst = parts[2] if len(parts) > 2 else "system"
            apps.append({"id": parts[0], "origin": origin, "installation": inst})
    return apps

data = {
    "dnf_user_packages": lines("dnf-user-packages.txt"),
    "flatpak_apps": flatpak_apps(),
    "snap_packages": snap_names(),
    "npm_global": lines("npm-global-packages.txt"),
    "npm_user": lines("npm-user-packages.txt"),
    "pip_user_file": "pip-user.txt" if os.path.isfile(os.path.join(m, "pip-user.txt")) else None,
    "pipx": lines("pipx-packages.txt"),
    "cargo_crates": lines("cargo-crates.txt"),
    "cursor_extensions": lines("cursor-extensions.txt"),
    "cli_on_path": lines("cli-on-path.txt"),
    "local_bin": lines("local-bin.txt"),
    "special": {},
}
sp = os.path.join(m, "special.json")
if os.path.isfile(sp):
    try:
        data["special"] = json.load(open(sp))
    except Exception:
        pass
json.dump(data, open(out, "w"), indent=2)
PY
}

_backup_manifests() {
  local dest="$1"
  local m="$dest/manifests"
  mkdir -p "$m/dnf-repos" "$m/bin-scripts" "$m/sddm-themes"

  # Repos (third-party-ish: skip stock fedora if desired — copy all non-fedora-cisco defaults that look third party)
  local f
  for f in /etc/yum.repos.d/*.repo; do
    [[ -f "$f" ]] || continue
    cp -a "$f" "$m/dnf-repos/" 2>/dev/null || true
  done

  rpm -E %fedora > "$m/fedora-release.txt" 2>/dev/null || true
  cp -a /etc/os-release "$m/os-release.txt" 2>/dev/null || true

  if ! dnf repoquery --userinstalled -q --queryformat '%{name}\n' 2>/dev/null \
        | sort -u > "$m/dnf-user-packages.txt"; then
    dnf repoquery --userinstalled -q --qf '%{name}\n' 2>/dev/null \
      | sort -u > "$m/dnf-user-packages.txt" || true
  fi
  # Also keep NEVRAs
  dnf repoquery --userinstalled -q 2>/dev/null > "$m/dnf-user-packages-nevra.txt" || true

  _write_hw_inventory "$m/hw-inventory.txt"
  _write_drivers_manifest "$dest"

  flatpak remotes 2>/dev/null > "$m/flatpak-remotes.txt" || true
  flatpak list --app --columns=application,origin,installation 2>/dev/null > "$m/flatpak-apps.txt" || true
  # Clean one-id-per-line list for restore
  flatpak list --app --columns=application 2>/dev/null | grep -v '^Application$' | sort -u > "$m/flatpak-app-ids.txt" || true
  snap list 2>/dev/null > "$m/snap-packages.txt" || true
  snap list 2>/dev/null | awk 'NR>1 {print $1}' > "$m/snap-package-names.txt" || true

  # Verbose npm trees (human) + clean installable package-name lists (restore)
  npm list -g --depth=0 2>/dev/null > "$m/npm-global.txt" || true
  npm list -g --prefix "$HOME/.local" --depth=0 2>/dev/null > "$m/npm-user.txt" || true
  _npm_package_names > "$m/npm-global-packages.txt"
  _npm_package_names --prefix "$HOME/.local" > "$m/npm-user-packages.txt"

  python3 -m pip freeze --user 2>/dev/null > "$m/pip-user.txt" || true
  if command -v pipx &>/dev/null; then
    pipx list --short 2>/dev/null > "$m/pipx-packages.txt" || true
    pipx list 2>/dev/null > "$m/pipx-list.txt" || true
  else
    : > "$m/pipx-packages.txt"
  fi

  cargo install --list 2>/dev/null > "$m/cargo-bins.txt" || true
  cargo install --list 2>/dev/null | awk '/^[^ ]/{print $1}' | sort -u > "$m/cargo-crates.txt" || true
  rustup show 2>/dev/null > "$m/rustup.txt" || true
  rustup toolchain list 2>/dev/null > "$m/rustup-toolchains.txt" || true
  {
    echo "nvm_dir=${NVM_DIR:-$HOME/.nvm}"
    command -v node >/dev/null && echo "node=$(node -v)"
    command -v npm >/dev/null && echo "npm=$(npm -v)"
    # Default / current alias for restore
    if [[ -s "${NVM_DIR:-$HOME/.nvm}/alias/default" ]]; then
      echo "default=$(cat "${NVM_DIR:-$HOME/.nvm}/alias/default")"
    fi
  } > "$m/nvm.txt" 2>/dev/null || true

  # PATH CLI inventory — binaries we care about reinstalling / verifying
  {
    for cmd in \
      node npm npx yarn pnpm bun deno \
      python3 pip pipx uv poetry \
      rustc cargo rustup \
      go java mvn gradle \
      docker podman gh aws gcloud kubectl terraform \
      claude supabase railway cursor code \
      flatpak snap fwupdmgr dnf \
      gem composer php flutter dart \
      lm-studio steam discord spotify \
      expo eas netlify vercel snyk; do
      if command -v "$cmd" &>/dev/null; then
        printf '%s\t%s\n' "$cmd" "$(command -v "$cmd")"
      fi
    done
  } > "$m/cli-on-path.txt"

  ls -1 "$HOME/.local/bin" 2>/dev/null | sort > "$m/local-bin.txt" || true
  ls -1 "$HOME/.cargo/bin" 2>/dev/null | sort > "$m/cargo-bin-names.txt" || true

  # Quoted heredoc + argv: an unquoted one would let a backup path containing a
  # quote or backslash inject into the generated Python.
  python3 - "$m/special.json" <<'PY'
import json, shutil, subprocess, sys
def ver(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip().split("\n")[0]
    except Exception:
        return ""
def which(name):
    return shutil.which(name) or ""
json.dump({
    "cursor": ver("rpm -q --qf '%{VERSION}' cursor"),
    "claude": ver("claude --version | head -1"),
    "supabase": ver("supabase --version | head -1"),
    "railway": ver("railway --version | head -1"),
    "gh": ver("gh --version | head -1"),
    "paths": {
        "claude": which("claude"),
        "supabase": which("supabase"),
        "railway": which("railway"),
        "cursor": which("cursor"),
        "gh": which("gh"),
    },
    "install_hints": {
        "claude": "npm i -g @anthropic-ai/claude-code  OR  claude install method used on source",
        "supabase": "curl -fsSL github.com/supabase/cli/releases/latest → ~/.local/bin/supabase",
        "railway": "npm i -g @railway/cli  OR  curl install script",
        "cursor": "dnf install cursor (cursor.repo) or RPM from downloads.cursor.com",
        "gh": "dnf install gh",
    },
}, open(sys.argv[1], "w"), indent=2)
PY

  ls -1 "$HOME/.local/share/applications/"*.desktop 2>/dev/null > "$m/desktop-files.txt" || true
  grep -h '^Name=\|^Exec=.*app-id=' "$HOME/.local/share/applications"/chrome-*.desktop 2>/dev/null > "$m/chrome-pwas.txt" || true

  id -nG > "$m/user-groups.txt" 2>/dev/null || true
  grep -E '^GRUB_CMDLINE' /etc/default/grub 2>/dev/null > "$m/grub-cmdline.txt" || true

  # bin scripts + this lib + desktop launcher
  rsync -a --exclude='lib/fedora-setup-backup.sh' "$HOME/bin/" "$m/bin-scripts/bin/" 2>/dev/null || true
  mkdir -p "$m/bin-scripts/bin/lib"
  cp -a "$_FEDORA_SETUP_LIB_DIR/fedora-setup-backup.sh" "$m/bin-scripts/bin/lib/" 2>/dev/null || true
  cp -a "$_FEDORA_SETUP_SCRIPT" "$m/bin-scripts/bin/" 2>/dev/null || true
  _copy_file "$HOME/.local/share/applications/check-fedora-updates.desktop" \
    "$m/bin-scripts/check-fedora-updates.desktop"
  [[ -x "$HOME/immich-go" ]] && _copy_file "$HOME/immich-go" "$m/bin-scripts/immich-go"

  # SDDM themes (non-RPM)
  for theme in Noir-SDDM-6 Nordic-Plasma-6 sweet-plasma6; do
    if [[ -d "/usr/share/sddm/themes/$theme" ]]; then
      rsync -a "/usr/share/sddm/themes/$theme" "$m/sddm-themes/" 2>/dev/null || true
    fi
  done
  # also copy any other themes not owned by rpm
  if command -v rpm &>/dev/null; then
    local theme_dir theme_base
    for theme_dir in /usr/share/sddm/themes/*/; do
      [[ -d "$theme_dir" ]] || continue
      theme_base="$(basename "$theme_dir")"
      [[ -e "$m/sddm-themes/$theme_base" ]] && continue
      if ! rpm -qf "$theme_dir" &>/dev/null; then
        rsync -a "$theme_dir" "$m/sddm-themes/" 2>/dev/null || true
      fi
    done
  fi

  # Cursor / VS Code multi-root workspace files (e.g. ~/bin/causeandeffects-app.code-workspace)
  : > "$m/cursor-workspaces.txt"
  local ws
  while IFS= read -r -d '' ws; do
    echo "${ws#"$HOME"/}" >> "$m/cursor-workspaces.txt"
  done < <(find "$HOME/bin" "$HOME/Documents" "$HOME/Desktop" -maxdepth 2 -type f -name '*.code-workspace' -print0 2>/dev/null)

  # Android SDK list
  if command -v sdkmanager &>/dev/null; then
    sdkmanager --list_installed 2>/dev/null > "$m/android-sdk-packages.txt" || true
  fi

  {
    echo "=== localectl ==="
    localectl status 2>/dev/null || true
    echo "=== hostnamectl ==="
    hostnamectl 2>/dev/null || true
  } > "$m/system-locale.txt"

  firewall-cmd --list-all 2>/dev/null > "$m/firewalld.txt" || true
  firewall-cmd --list-all-zones 2>/dev/null > "$m/firewalld-zones.txt" || true

  # Cursor extensions (also refreshed in _backup_config if cursor is available later)
  if command -v cursor &>/dev/null; then
    cursor --list-extensions 2>/dev/null > "$m/cursor-extensions.txt" || true
  else
    : > "$m/cursor-extensions.txt"
  fi

  # Master programs/CLIs inventory (human + JSON) — must run after the lists above
  _write_programs_inventory "$m"
}

_backup_projects() {
  local dest="$1"
  local list="$dest/manifests/projects.txt"
  local roots_file="$dest/manifests/project-roots.txt"
  local pdir="$dest/projects"
  local depth
  depth="$(_backup_project_depth)"
  mkdir -p "$pdir"
  : > "$list"
  : > "$roots_file"

  local root repo rel home_prefix="$HOME/"
  while IFS= read -r root; do
    [[ -d "$root" ]] || continue
    echo "$root" >> "$roots_file"

    # If the root itself is a git repo, archive it as one unit
    if [[ -d "$root/.git" ]]; then
      rel="${root#"$home_prefix"}"
      [[ "$rel" == "$root" ]] && rel="$(basename "$root")"
      {
        echo "path=$rel"
        git -C "$root" remote -v 2>/dev/null
        git -C "$root" status -sb 2>/dev/null | head -1
        echo "---"
      } >> "$list"
      mkdir -p "$pdir/$(dirname "$rel")"
      rsync -a "${_RSYNC_PROJECT_EXCLUDES[@]}" "$root"/ "$pdir/$rel"/ 2>/dev/null || true
      continue
    fi

    # Otherwise find nested git roots up to configured depth
    while IFS= read -r -d '' repo; do
      repo="$(dirname "$repo")"
      rel="${repo#"$home_prefix"}"
      [[ "$rel" == "$repo" ]] && rel="$(basename "$repo")"
      {
        echo "path=$rel"
        git -C "$repo" remote -v 2>/dev/null
        git -C "$repo" status -sb 2>/dev/null | head -1
        echo "---"
      } >> "$list"
      mkdir -p "$pdir/$(dirname "$rel")"
      rsync -a "${_RSYNC_PROJECT_EXCLUDES[@]}" "$repo"/ "$pdir/$rel"/ 2>/dev/null || true
    done < <(find "$root" -maxdepth "$depth" -type d -name .git -print0 2>/dev/null)
  done < <(_backup_project_roots)
}

# Paths the user added in the Backup UI (or backup_extra_paths in config).
_backup_collect_extra_paths() {
  local -A seen=()
  local line item cfgf conf_raw
  if [[ -n "${URSTACK_BACKUP_EXTRA_PATHS:-}" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line#"${line%%[![:space:]]*}"}"
      line="${line%"${line##*[![:space:]]}"}"
      [[ -z "$line" || "$line" == \#* ]] && continue
      [[ -n "${seen[$line]:-}" ]] && continue
      seen[$line]=1
      printf '%s\n' "$line"
    done <<< "$URSTACK_BACKUP_EXTRA_PATHS"
  fi
  cfgf="${XDG_CONFIG_HOME:-$HOME/.config}/urstack/backup-extra-paths.conf"
  if [[ -f "$cfgf" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line#"${line%%[![:space:]]*}"}"
      line="${line%"${line##*[![:space:]]}"}"
      [[ -z "$line" || "$line" == \#* ]] && continue
      [[ -n "${seen[$line]:-}" ]] && continue
      seen[$line]=1
      printf '%s\n' "$line"
    done < "$cfgf"
  fi
  conf_raw="$(_backup_cfg backup_extra_paths "")"
  if [[ -n "$conf_raw" ]]; then
    local -a parts=()
    IFS=':,' read -ra parts <<< "$conf_raw"
    for item in "${parts[@]}"; do
      item="${item#"${item%%[![:space:]]*}"}"
      item="${item%"${item##*[![:space:]]}"}"
      [[ -z "$item" ]] && continue
      if [[ "$item" != /* ]]; then
        item="$HOME/$item"
      fi
      [[ -n "${seen[$item]:-}" ]] && continue
      seen[$item]=1
      printf '%s\n' "$item"
    done
  fi
}

# Archive user-selected folders/files into $dest/extra/ (home-relative layout).
_backup_extra_user_paths() {
  local dest="$1"
  local list="$dest/manifests/extra-paths.txt"
  local edir="$dest/extra"
  local src rel home_prefix="$HOME/"
  local any=0
  mkdir -p "$edir" "$dest/manifests"
  : > "$list"

  while IFS= read -r src || [[ -n "$src" ]]; do
    [[ -n "$src" ]] || continue
    if [[ "$src" == ~* ]]; then
      src="${src/#\~/$HOME}"
    fi
    if [[ ! -e "$src" && ! -L "$src" ]]; then
      echo "# skip missing: $src" >> "$list"
      continue
    fi
    rel="${src#"$home_prefix"}"
    if [[ "$rel" == "$src" ]]; then
      # Outside $HOME — keep under extra/_outside/<basename or hashed path>
      rel="_outside/$(echo "$src" | sed 's#^/##; s#/#_#g')"
    fi
    any=1
    if [[ -d "$src" && ! -L "$src" ]]; then
      mkdir -p "$edir/$(dirname "$rel")"
      rsync -a "${_RSYNC_PROJECT_EXCLUDES[@]}" "$src"/ "$edir/$rel"/ 2>/dev/null || true
      echo "dir=$rel" >> "$list"
    else
      mkdir -p "$edir/$(dirname "$rel")"
      cp -a "$src" "$edir/$rel" 2>/dev/null || true
      echo "file=$rel" >> "$list"
    fi
  done < <(_backup_collect_extra_paths)

  if [[ $any -eq 0 ]]; then
    rmdir "$edir" 2>/dev/null || true
  fi
}

# AppImages + vendor/outside-store launchers (for fresh-install rebuild)
_backup_appimages_and_vendor() {
  local dest="$1"
  local m="$dest/manifests"
  local adir="$dest/appimages"
  mkdir -p "$adir" "$m"
  local list="$m/appimages.txt"
  local vendor="$m/vendor-launchers.txt"
  : > "$list"
  : > "$vendor"

  local f name destf
  for f in "$HOME/Applications"/*.AppImage "$HOME/.local/bin"/*.AppImage; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    destf="$adir/$name"
    if cp -a "$f" "$destf" 2>/dev/null; then
      echo "ok|$name|${f#"$HOME"/}|$destf" >> "$list"
    else
      echo "fail|$name|${f#"$HOME"/}|" >> "$list"
    fi
  done

  # Desktop files whose Exec points outside system/flatpak paths → vendor note
  local desk exec_line exec_bin
  while IFS= read -r -d '' desk; do
    exec_line=$(grep -E '^Exec=' "$desk" 2>/dev/null | head -1 | cut -d= -f2-) || true
    [[ -n "$exec_line" ]] || continue
    exec_bin="${exec_line%% *}"
    exec_bin="${exec_bin#\"}"
    exec_bin="${exec_bin%\"}"
    case "$exec_bin" in
      /usr/*|/bin/*|/sbin/*|flatpak|snap|env|sh|bash|python*|gio) continue ;;
      *AppImage|*appimage)
        echo "appimage|$(basename "$desk")|$exec_bin" >> "$vendor"
        ;;
      "$HOME"/*|/opt/*)
        echo "vendor|$(basename "$desk")|$exec_bin" >> "$vendor"
        ;;
    esac
  done < <(find "$HOME/.local/share/applications" -maxdepth 1 -type f -name '*.desktop' -print0 2>/dev/null)

  # UrStack catalog apps that look installed via browser/vendor (detect command exists)
  if [[ -f "$_FEDORA_SETUP_ROOT/lib/core/catalog.sh" ]]; then
    # shellcheck source=/dev/null
    source "$_FEDORA_SETUP_ROOT/lib/core/catalog.sh" 2>/dev/null || true
    if declare -F catalog_status_file &>/dev/null; then
      local st
      st=$(mktemp)
      URSTACK_ROOT="$_FEDORA_SETUP_ROOT" STACKUP_ROOT="$_FEDORA_SETUP_ROOT" FEDORA_UPDATES_ROOT="$_FEDORA_SETUP_ROOT" catalog_status_file "$st" 2>/dev/null || true
      awk -F'|' '$6=="browser" && $8=="1" {print "catalog|"$2"|"$9}' "$st" 2>/dev/null >> "$vendor" || true
      rm -f "$st"
    fi
  fi
}

_restore_appimages_and_vendor() {
  local dest="$1"
  local adir="$dest/appimages"
  local list="$dest/manifests/appimages.txt"
  local apps_dir="$HOME/Applications"
  mkdir -p "$apps_dir" "$HOME/.local/share/applications"

  if [[ -d "$adir" ]]; then
    local f name
    for f in "$adir"/*.AppImage; do
      [[ -f "$f" ]] || continue
      name="$(basename "$f")"
      if cp -a "$f" "$apps_dir/$name" 2>/dev/null; then
        chmod +x "$apps_dir/$name" 2>/dev/null || true
        # Ensure a simple desktop entry exists
        if [[ ! -f "$HOME/.local/share/applications/${name%.AppImage}.desktop" ]]; then
          cat > "$HOME/.local/share/applications/${name%.AppImage}.desktop" <<EOF
[Desktop Entry]
Name=${name%.AppImage}
Exec=$apps_dir/$name
Icon=application-x-executable
Type=Application
Categories=Utility;
Terminal=false
EOF
        fi
        _restore_ok "AppImage: $name"
      else
        _restore_fail "AppImage: $name"
      fi
    done
  elif [[ -f "$list" ]]; then
    _restore_skip "AppImages (none in backup)"
  fi

  # Copy vendor note into home for the user
  if [[ -f "$dest/manifests/vendor-launchers.txt" ]]; then
    mkdir -p "$HOME/.local/share/urstack"
    cp -a "$dest/manifests/vendor-launchers.txt" \
      "$HOME/.local/share/urstack/vendor-launchers-from-backup.txt" 2>/dev/null || true
    _restore_ok "Vendor launcher list copied to ~/.local/share/urstack/"
  fi
}

_backup_config() {
  local dest="$1"
  local ov="$dest/config/home-overlay"
  mkdir -p "$ov" "$dest/config/etc" "$dest/config/firefox-bookmarks"

  local want_settings want_secrets want_browsers want_system want_full
  want_settings="$(_backup_include settings 1)"
  want_secrets="$(_backup_include secrets 1)"
  want_browsers="$(_backup_include browsers 1)"
  want_system="$(_backup_include system 1)"
  want_full="$(_backup_include full_dotconfig 1)"

  if [[ "$want_settings" == "1" ]]; then
  # KDE / Plasma
  local kde_files=(
    .config/plasma-org.kde.plasma.desktop-appletsrc
    .config/plasmashellrc .config/plasmarc .config/kdeglobals
    .config/kwinrc .config/kwinrulesrc .config/kwinoutputconfig.json
    .config/kded5rc .config/kded6rc .config/plasma-localerc
    .config/plasmanotifyrc .config/plasmaparc .config/plasma-workspace
    .config/kscreenlockerrc .config/kglobalshortcutsrc .config/khotkeysrc
    .config/dolphinrc .config/kcminputrc .config/touchpadxlibinputrc
    .config/powermanagementprofilesrc .config/powerdevilrc
    .config/mimeapps.list .config/konsolerc .config/klipperrc .config/spectaclerc
    .config/kwalletrc .config/kwalletmanagerrc
  )
  local p
  for p in "${kde_files[@]}"; do
    _overlay_home "$HOME/$p" "$dest"
  done

  _overlay_home "$HOME/.local/share/plasma" "$dest"
  _overlay_home "$HOME/.local/share/aurorae" "$dest"
  _overlay_home "$HOME/.local/share/color-schemes" "$dest"
  _overlay_home "$HOME/.local/share/icons" "$dest"
  _overlay_home "$HOME/.local/share/wallpapers" "$dest"
  _overlay_home "$HOME/.local/share/konsole" "$dest"
  _overlay_home "$HOME/.local/share/kscreen" "$dest"
  _overlay_home "$HOME/.local/share/dolphin" "$dest"
  _overlay_home "$HOME/.local/share/user-places.xbel" "$dest"
  _overlay_home "$HOME/.local/share/kwalletd" "$dest"
  _overlay_home "$HOME/.local/share/flatpak/overrides" "$dest"
  _overlay_home "$HOME/.local/share/applications" "$dest"
  _overlay_home "$HOME/.icons" "$dest"

  # GTK / GNOME extras
  _overlay_home "$HOME/.config/gtk-3.0" "$dest"
  _overlay_home "$HOME/.config/gtk-4.0" "$dest"
  _overlay_home "$HOME/.gtkrc-2.0" "$dest"
  _overlay_home "$HOME/.gtkrc-2.0-kde4" "$dest"
  _overlay_home "$HOME/.fonts.conf" "$dest"
  _overlay_home "$HOME/.config/xsettingsd" "$dest"
  _overlay_home "$HOME/.config/nushell" "$dest"
  _overlay_home "$HOME/.config/dconf" "$dest"
  _overlay_home "$HOME/.config/gnome-control-center" "$dest"
  fi

  # Secrets / identity (optional)
  if [[ "$want_secrets" == "1" ]]; then
    _overlay_home "$HOME/.ssh" "$dest"
    _overlay_home "$HOME/.gnupg" "$dest"
    _overlay_home "$HOME/.netrc" "$dest"
    _overlay_home "$HOME/.git-credentials" "$dest"
    _overlay_home "$HOME/.config/gh" "$dest"
  fi
  if [[ "$want_settings" == "1" ]]; then
  _overlay_home "$HOME/.gitconfig" "$dest"
  _overlay_home "$HOME/.config/git" "$dest"

  # Claude / Warp / apps
  _overlay_home "$HOME/.claude.json" "$dest"
  _overlay_home "$HOME/.claude" "$dest"
  _overlay_home "$HOME/.config/warp-terminal" "$dest"
  _overlay_home "$HOME/.config/libreoffice" "$dest"
  _overlay_home "$HOME/.config/kate" "$dest"
  _overlay_home "$HOME/.config/vlc" "$dest"
  _overlay_home "$HOME/.config/qBittorrent" "$dest"
  _overlay_home "$HOME/.config/kdeconnect" "$dest"
  _overlay_home "$HOME/.config/input-remapper-2" "$dest"
  _overlay_home "$HOME/.config/autostart" "$dest"
  _overlay_home "$HOME/.config/systemd/user" "$dest"
  _overlay_home "$HOME/.nvidia-settings-rc" "$dest"
  _overlay_home "$HOME/.local/state/wireplumber" "$dest"

  # Shell
  _overlay_home "$HOME/.bashrc" "$dest"
  _overlay_home "$HOME/.bash_profile" "$dest"
  _overlay_home "$HOME/.profile" "$dest"
  _overlay_home "$HOME/.zshrc" "$dest"
  _overlay_home "$HOME/.zprofile" "$dest"
  _overlay_home "$HOME/.inputrc" "$dest"

  # Cursor settings only
  mkdir -p "$ov/.config/Cursor/User"
  for p in settings.json keybindings.json snippets; do
    _copy_file "$HOME/.config/Cursor/User/$p" "$ov/.config/Cursor/User/$p"
  done
  if command -v cursor &>/dev/null; then
    cursor --list-extensions 2>/dev/null > "$dest/manifests/cursor-extensions.txt" || true
  fi

  # Cursor / VS Code workspace files under project roots + bin
  local ws search_dirs=("$HOME/bin")
  while IFS= read -r root; do
    search_dirs+=("$root")
  done < <(_backup_project_roots)
  while IFS= read -r -d '' ws; do
    _overlay_home "$ws" "$dest"
  done < <(find "${search_dirs[@]}" -maxdepth 2 -type f -name '*.code-workspace' -print0 2>/dev/null)
  fi

  # Chrome PWAs + bookmarks + Web Applications
  if [[ "$want_browsers" == "1" ]]; then
    _overlay_home "$HOME/.config/google-chrome/Default/Bookmarks" "$dest"
    _overlay_home "$HOME/.config/google-chrome/Default/Bookmarks.bak" "$dest"
    _overlay_home "$HOME/.config/google-chrome/Default/Web Applications" "$dest"
  fi

  if [[ "$want_settings" == "1" ]]; then
  # Resolve wallpaper / menu icon paths from appletsrc + lock screen
  local img
  for img in $(grep -hE '^(Image|customButtonImage|PreviewImage)=' \
      "$HOME/.config/plasma-org.kde.plasma.desktop-appletsrc" \
      "$HOME/.config/kscreenlockerrc" 2>/dev/null \
      | sed 's/^[^=]*=//;s|^file://||' | sort -u); do
    [[ -f "$img" ]] || continue
    if [[ "$img" == "$HOME"/* ]]; then
      _overlay_home "$img" "$dest"
    else
      mkdir -p "$dest/config/extra-media"
      cp -a "$img" "$dest/config/extra-media/" 2>/dev/null || true
    fi
  done
  fi
  if [[ "$want_browsers" == "1" ]]; then
    for img in $(grep -h '^Icon=/' "$HOME/.local/share/applications"/chrome-*.desktop 2>/dev/null | cut -d= -f2-); do
      [[ -f "$img" ]] || continue
      if [[ "$img" == "$HOME"/* ]]; then
        _overlay_home "$img" "$dest"
      fi
    done

    # Firefox bookmarks (places)
    local ffprofile
    ffprofile=$(find "$HOME/.mozilla/firefox" -maxdepth 1 -type d -name '*.default-release' 2>/dev/null | head -1)
    if [[ -n "$ffprofile" ]]; then
      _copy_file "$ffprofile/places.sqlite" "$dest/config/firefox-bookmarks/places.sqlite"
      _copy_file "$ffprofile/favicons.sqlite" "$dest/config/firefox-bookmarks/favicons.sqlite"
      basename "$ffprofile" > "$dest/config/firefox-bookmarks/profile-name.txt"
    fi
  fi

  # Broader ~/.config + ~/.local/share (caches excluded) — makes restore closer to "same machine"
  if [[ "$want_full" == "1" ]]; then
    mkdir -p "$ov/.config" "$ov/.local/share"
    local -a cfg_excludes=(
      --exclude='**/Cache/'
      --exclude='**/CacheStorage/'
      --exclude='**/Code Cache/'
      --exclude='**/GPUCache/'
      --exclude='**/ShaderCache/'
      --exclude='**/DawnCache/'
      --exclude='**/GrShaderCache/'
      --exclude='**/Crashpad/'
      --exclude='**/Crash Reports/'
      --exclude='**/CachedData/'
      --exclude='**/CachedExtensionVSIXs/'
      --exclude='**/logs/'
      --exclude='**/log/'
      --exclude='google-chrome/Default/Service Worker/'
      --exclude='google-chrome/Default/IndexedDB/'
      --exclude='google-chrome/Default/File System/'
      --exclude='google-chrome/Default/Local Storage/'
      --exclude='google-chrome/Default/Session Storage/'
      --exclude='BraveSoftware/'
      --exclude='microsoft-edge/'
      --exclude='chromium/'
      --exclude='Cursor/CachedData/'
      --exclude='Cursor/Cache/'
      --exclude='Cursor/GPUCache/'
      --exclude='Code/Cache/'
      --exclude='Code/CachedData/'
      --exclude='Slack/Cache/'
      --exclude='discord/Cache/'
      --exclude='spotify/'
    )
    if [[ "$want_secrets" != "1" ]]; then
      cfg_excludes+=(--exclude='gh/' --exclude='git-credentials')
    fi
    if [[ "$want_browsers" != "1" ]]; then
      cfg_excludes+=(--exclude='google-chrome/')
    fi
    rsync -a "${cfg_excludes[@]}" \
      "$HOME/.config"/ "$ov/.config"/ 2>/dev/null || true

    local -a share_excludes=(
      --exclude='Trash/'
      --exclude='flatpak/app/'
      --exclude='flatpak/runtime/'
      --exclude='Steam/'
      --exclude='lutris/'
      --exclude='containers/'
      --exclude='docker/'
      --exclude='libvirt/'
      --exclude='waydroid/'
      --exclude='Trash'
      --exclude='gvfs-metadata/'
      --exclude='baloo/'
      --exclude='tracker3/'
      --exclude='webkitgtk/'
      --exclude='xorg/'
    )
    rsync -a "${share_excludes[@]}" \
      "$HOME/.local/share"/ "$ov/.local/share"/ 2>/dev/null || true
  fi

  if [[ "$want_settings" == "1" && -d "$HOME/refind-theme-regular" ]]; then
    _overlay_home "$HOME/refind-theme-regular" "$dest"
  fi

  # System snippets
  if [[ "$want_system" == "1" ]]; then
    mkdir -p "$dest/config/etc/sysctl.d" "$dest/config/etc/modules-load.d" "$dest/config/etc/sddm.conf.d" "$dest/config/etc/cups/ppd"
    _copy_file /etc/default/grub "$dest/config/etc/default/grub"
    if [[ -d /etc/sddm.conf.d ]]; then
      rsync -a /etc/sddm.conf.d/ "$dest/config/etc/sddm.conf.d/" 2>/dev/null || true
    fi
    _copy_file /etc/sddm.conf "$dest/config/etc/sddm.conf"
    _copy_file /etc/sysctl.d/50-cursor.conf "$dest/config/etc/sysctl.d/50-cursor.conf"
    _copy_file /etc/modules-load.d/uhid.conf "$dest/config/etc/modules-load.d/uhid.conf"
    _copy_file /etc/cups/printers.conf "$dest/config/etc/cups/printers.conf"
    _copy_file /etc/cups/lpoptions "$dest/config/etc/cups/lpoptions"
    if [[ -d /etc/cups/ppd ]]; then
      rsync -a /etc/cups/ppd/ "$dest/config/etc/cups/ppd/" 2>/dev/null || true
    fi

    # Snap user data
    if [[ -d "$HOME/snap" ]]; then
      mkdir -p "$dest/config/snap"
      rsync -a --exclude='*/common/.cache/' --exclude='*/.config/*/Cache/' \
        "$HOME/snap"/ "$dest/config/snap"/ 2>/dev/null || true
    fi

    # `crontab -l` fails but still leaves a zero-byte file when there is no
    # crontab, and `crontab <empty file>` on restore deletes the target's.
    crontab -l > "$dest/manifests/user-crontab.txt" 2>/dev/null \
      || rm -f "$dest/manifests/user-crontab.txt"
    [[ -s "$dest/manifests/user-crontab.txt" ]] || rm -f "$dest/manifests/user-crontab.txt"
  fi
}

# Build a complete inventory of everything present in the backup tree.
# Writes BACKUP_MANIFEST.md (full) and returns summary text for the dialog.
_generate_backup_inventory() {
  local dest="$1"
  python3 - "$dest" <<'PY'
import json, os, sys
from pathlib import Path

dest = Path(sys.argv[1])
m = dest / "manifests"
cfg = dest / "config"
ov = cfg / "home-overlay"
proj = dest / "projects"

def sz(p: Path) -> str:
    if not p.exists():
        return "—"
    total = 0
    if p.is_file():
        total = p.stat().st_size
    else:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    for unit in ("B", "K", "M", "G", "T"):
        if total < 1024 or unit == "T":
            if unit == "B":
                return f"{total}B"
            return f"{total:.1f}{unit}" if total < 10 and unit != "K" else f"{total:.0f}{unit}"
        total /= 1024
    return "?"

def lines(path: Path, skip_hash=True, skip_blank=True):
    if not path.is_file():
        return []
    out = []
    with path.open(errors="replace") as f:
        for ln in f:
            s = ln.rstrip("\n")
            if skip_blank and not s.strip():
                continue
            if skip_hash and s.lstrip().startswith("#"):
                continue
            out.append(s)
    return out

def list_names(path: Path):
    return [ln.split()[0] for ln in lines(path) if ln.split()]

def present(rel: str) -> bool:
    return (ov / rel).exists() or (cfg / rel).exists()

sections = []
summary_bits = []

# --- Header ---
manifest = []
manifest.append("# Fedora setup backup — full inventory")
manifest.append("")
manifest.append(f"- Created: from backup at `{dest}`")
manifest.append(f"- Host inventory files under `manifests/`")
manifest.append(f"- **Total size:** {sz(dest)}")
manifest.append("")

# --- Top-level sizes ---
manifest.append("## Top-level")
manifest.append("")
manifest.append("| Path | Size |")
manifest.append("|------|------|")
for name in sorted(os.listdir(dest)):
    p = dest / name
    if name.startswith("."):
        continue
    manifest.append(f"| `{name}` | {sz(p)} |")
manifest.append("")

# --- Programs & packages ---
manifest.append("## Programs & packages (reinstall lists)")
manifest.append("")

dnf = lines(m / "dnf-user-packages.txt")
flatpak = lines(m / "flatpak-apps.txt")
flatpak = [ln for ln in flatpak if not ln.lower().startswith("application")]
snaps = list_names(m / "snap-package-names.txt") or [
    ln.split()[0] for i, ln in enumerate(lines(m / "snap-packages.txt", skip_hash=False)) if i > 0 and ln.split()
]
npm_g = lines(m / "npm-global-packages.txt")
npm_u = lines(m / "npm-user-packages.txt")
pip = lines(m / "pip-user.txt")
pipx = lines(m / "pipx-packages.txt")
cargo = lines(m / "cargo-crates.txt")
clis = lines(m / "cli-on-path.txt")
exts = lines(m / "cursor-extensions.txt")
local_bin = lines(m / "local-bin.txt")
cargo_bins = lines(m / "cargo-bin-names.txt")
desktops = lines(m / "desktop-files.txt")
repos = list((m / "dnf-repos").glob("*.repo")) if (m / "dnf-repos").is_dir() else []
themes = [p.name for p in (m / "sddm-themes").iterdir()] if (m / "sddm-themes").is_dir() else []
bin_scripts = []
bs = m / "bin-scripts" / "bin"
if bs.is_dir():
    bin_scripts = sorted([p.name for p in bs.rglob("*") if p.is_file()])[:200]

def dump_list(title, items, bullet=True):
    manifest.append(f"### {title} ({len(items)})")
    manifest.append("")
    if not items:
        manifest.append("_(none)_")
        manifest.append("")
        return
    for it in items:
        manifest.append(f"- {it}" if bullet else it)
    manifest.append("")

dump_list("DNF user packages", dnf)
dump_list("Flatpak apps", flatpak)
dump_list("Snap packages", snaps)
dump_list("npm global (nvm)", npm_g)
dump_list("npm user (~/.local)", npm_u)
dump_list("pip user packages", pip)
dump_list("pipx tools", pipx)
dump_list("Cargo crates", cargo)
dump_list("Cursor extensions", exts)
dump_list("CLIs on PATH", clis)
dump_list("~/.local/bin entrypoints", local_bin)
dump_list("~/.cargo/bin", cargo_bins)
dump_list("Yum/DNF repo files", [p.name for p in repos])
dump_list("SDDM themes (copied)", themes)
dump_list("User .desktop launchers", [Path(x).name for x in desktops])

# Special CLIs
special = {}
sp = m / "special.json"
if sp.is_file():
    try:
        special = json.loads(sp.read_text())
    except Exception:
        special = {}
manifest.append("### Special CLIs")
manifest.append("")
if special:
    for k in ("cursor", "claude", "supabase", "railway", "gh"):
        if special.get(k):
            manifest.append(f"- **{k}:** {special[k]}")
    paths = special.get("paths") or {}
    if paths:
        manifest.append("")
        manifest.append("Paths:")
        for k, v in paths.items():
            if v:
                manifest.append(f"- {k}: `{v}`")
else:
    manifest.append("_(none)_")
manifest.append("")

# Drivers / hardware
manifest.append("### Drivers / hardware")
manifest.append("")
drv = m / "drivers.txt"
if drv.is_file():
    for ln in lines(drv, skip_hash=False):
        manifest.append(f"    {ln}" if not ln.startswith("#") else ln)
else:
    manifest.append("_(none)_")
manifest.append("")

# Android SDK
sdk = lines(m / "android-sdk-packages.txt")
if sdk:
    dump_list("Android SDK packages", sdk[:80])

# Chrome PWAs
pwas = lines(m / "chrome-pwas.txt")
if pwas:
    dump_list("Chrome PWA markers", pwas)

# --- Projects ---
manifest.append("## Project repositories")
manifest.append("")
project_names = []
if proj.is_dir():
    for gitdir in sorted(proj.rglob(".git")):
        if gitdir.is_dir():
            rel = gitdir.parent.relative_to(proj).as_posix()
            project_names.append(f"{rel} ({sz(gitdir.parent)})")
if project_names:
    for n in project_names:
        manifest.append(f"- {n}")
else:
    # fall back to projects.txt
    for ln in lines(m / "projects.txt", skip_hash=False):
        if ln.startswith("path=") or ln.startswith("Documents/") or ln == "waydroid_script":
            project_names.append(ln.replace("path=", ""))
            manifest.append(f"- {ln}")
    if not project_names:
        manifest.append("_(none)_")
manifest.append("")
manifest.append(f"Projects total size: **{sz(proj)}**")
manifest.append("")

# --- Config / home overlay ---
manifest.append("## Settings & data (config/)")
manifest.append("")
manifest.append(f"Config total size: **{sz(cfg)}**")
manifest.append("")

# Categorize overlay contents
categories = {
    "KDE / Plasma": [
        ".config/plasma-org.kde.plasma.desktop-appletsrc", ".config/plasmashellrc", ".config/plasmarc",
        ".config/kdeglobals", ".config/kwinrc", ".config/kwinrulesrc", ".config/kwinoutputconfig.json",
        ".config/kscreenlockerrc", ".config/kglobalshortcutsrc", ".config/dolphinrc",
        ".config/konsolerc", ".local/share/plasma", ".local/share/konsole", ".local/share/kscreen",
        ".local/share/aurorae", ".local/share/color-schemes", ".local/share/icons",
        ".local/share/wallpapers", ".local/share/dolphin", ".local/share/kwalletd",
    ],
    "GTK / fonts": [".config/gtk-3.0", ".config/gtk-4.0", ".gtkrc-2.0", ".fonts.conf", ".config/xsettingsd"],
    "Shell": [".bashrc", ".bash_profile", ".profile", ".config/nushell"],
    "Secrets / identity": [".ssh", ".gnupg", ".netrc", ".git-credentials", ".gitconfig", ".config/git", ".config/gh"],
    "Apps": [
        ".claude.json", ".claude", ".config/warp-terminal", ".config/libreoffice", ".config/kate",
        ".config/vlc", ".config/qBittorrent", ".config/kdeconnect", ".config/input-remapper-2",
        ".config/autostart", ".config/systemd/user", ".nvidia-settings-rc", ".local/state/wireplumber",
        ".local/share/applications", ".local/share/flatpak/overrides",
    ],
    "Cursor / VS Code": [".config/Cursor/User"],
    "Chrome bookmarks / PWAs": [
        ".config/google-chrome/Default/Bookmarks",
        ".config/google-chrome/Default/Bookmarks.bak",
        ".config/google-chrome/Default/Web Applications",
    ],
    "rEFInd theme": ["refind-theme-regular"],
}

manifest.append("### Home overlay (`config/home-overlay/`)")
manifest.append("")
overlay_present = []
for cat, paths in categories.items():
    found = []
    for rel in paths:
        p = ov / rel
        if p.exists():
            found.append(f"`{rel}` ({sz(p)})")
    if found:
        overlay_present.append(cat)
        manifest.append(f"**{cat}**")
        for f in found:
            manifest.append(f"- {f}")
        manifest.append("")

# Workspace files
workspaces = lines(m / "cursor-workspaces.txt")
if workspaces:
    manifest.append("**Cursor/VS Code workspaces**")
    for w in workspaces:
        manifest.append(f"- `{w}`")
    manifest.append("")

# Extra media / firefox / snap / etc
extra = cfg / "extra-media"
if extra.is_dir() and any(extra.iterdir()):
    manifest.append(f"**Extra media (wallpapers/icons):** {sz(extra)}")
    for p in sorted(extra.iterdir())[:40]:
        manifest.append(f"- `{p.name}` ({sz(p)})")
    manifest.append("")

ff = cfg / "firefox-bookmarks"
if ff.is_dir() and any(ff.iterdir()):
    manifest.append(f"**Firefox bookmarks:** {sz(ff)}")
    for p in sorted(ff.iterdir()):
        manifest.append(f"- `{p.name}`")
    manifest.append("")

snap = cfg / "snap"
if snap.is_dir() and any(snap.iterdir()):
    snap_apps = sorted([p.name for p in snap.iterdir() if p.is_dir()])
    manifest.append(f"**Snap user data:** {sz(snap)} — {len(snap_apps)} app(s)")
    for a in snap_apps:
        manifest.append(f"- {a}")
    manifest.append("")

# System etc
manifest.append("### System config (`config/etc/`)")
manifest.append("")
etc = cfg / "etc"
etc_items = []
if etc.is_dir():
    for p in sorted(etc.rglob("*")):
        if p.is_file():
            etc_items.append(p.relative_to(etc).as_posix())
if etc_items:
    for e in etc_items:
        manifest.append(f"- `/etc/{e}`")
else:
    manifest.append("_(none)_")
manifest.append("")

# Bin scripts summary
manifest.append("## Bin scripts & helpers")
manifest.append("")
manifest.append(f"Copied under `manifests/bin-scripts/` ({sz(m / 'bin-scripts')})")
manifest.append("")
if bin_scripts:
    for b in bin_scripts[:80]:
        manifest.append(f"- `{b}`")
    if len(bin_scripts) > 80:
        manifest.append(f"- … and {len(bin_scripts) - 80} more")
else:
    manifest.append("_(none)_")
manifest.append("")

# Other manifest side-files
manifest.append("## Other manifest files")
manifest.append("")
for p in sorted(m.iterdir()):
    if p.name in ("dnf-repos", "bin-scripts", "sddm-themes"):
        continue
    if p.is_file():
        manifest.append(f"- `{p.name}` ({sz(p)})")
manifest.append("")

manifest.append("---")
manifest.append("Restore uses these lists to reinstall packages/CLIs and rsync overlays back.")
manifest.append("")

(dest / "BACKUP_MANIFEST.md").write_text("\n".join(manifest) + "\n", encoding="utf-8")

# --- Dialog / BACKUP_SUMMARY.txt (concise but complete categories) ---
sum_lines = []
sum_lines.append("Backup complete — full inventory")
sum_lines.append("")
sum_lines.append(f"Location: {dest}")
sum_lines.append(f"Size:     {sz(dest)}")
sum_lines.append("")
sum_lines.append("Programs & packages")
sum_lines.append(f"• DNF user packages:     {len(dnf)}")
sum_lines.append(f"• Flatpak apps:          {len(flatpak)}")
sum_lines.append(f"• Snap packages:         {len(snaps)}")
sum_lines.append(f"• npm global (nvm):      {len(npm_g)}")
sum_lines.append(f"• npm user (~/.local):   {len(npm_u)}")
sum_lines.append(f"• pip user packages:     {len(pip)}")
sum_lines.append(f"• pipx tools:            {len(pipx)}")
sum_lines.append(f"• Cargo crates:          {len(cargo)}")
sum_lines.append(f"• CLIs on PATH:          {len(clis)}")
sum_lines.append(f"• ~/.local/bin tools:    {len(local_bin)}")
sum_lines.append(f"• Cursor extensions:     {len(exts)}")
sum_lines.append(f"• Desktop launchers:     {len(desktops)}")
sum_lines.append(f"• DNF repo files:        {len(repos)}")
sum_lines.append(f"• SDDM themes:           {len(themes)}")
sum_lines.append(f"• Bin scripts:           {len(bin_scripts)}")
sum_lines.append(f"• Project repos:         {len(project_names)}")
appimages = lines(m / "appimages.txt", skip_hash=False)
vendor = lines(m / "vendor-launchers.txt", skip_hash=False)
sum_lines.append(f"• AppImages archived:    {len([x for x in appimages if x.startswith('ok|')])}")
sum_lines.append(f"• Vendor launchers noted:{len(vendor)}")
roots = lines(m / "project-roots.txt", skip_hash=False)
if roots:
    sum_lines.append(f"• Project roots:         {', '.join(roots)}")
if project_names:
    sum_lines.append("")
    sum_lines.append("Projects:")
    for n in project_names:
        sum_lines.append(f"  – {n}")
if flatpak:
    sum_lines.append("")
    sum_lines.append("Flatpak apps:")
    for a in flatpak:
        sum_lines.append(f"  – {a.split()[0] if a.split() else a}")
if snaps:
    sum_lines.append("")
    sum_lines.append("Snap packages:")
    for a in snaps:
        sum_lines.append(f"  – {a}")
if npm_g or npm_u:
    sum_lines.append("")
    sum_lines.append("npm packages:")
    for a in npm_g:
        sum_lines.append(f"  – {a} (nvm global)")
    for a in npm_u:
        sum_lines.append(f"  – {a} (~/.local)")
if cargo:
    sum_lines.append("")
    sum_lines.append("Cargo crates:")
    for a in cargo:
        sum_lines.append(f"  – {a}")
if special:
    sum_lines.append("")
    bits = []
    for k in ("cursor", "claude", "supabase", "railway", "gh"):
        v = special.get(k) or ""
        if v:
            bits.append(f"{k} ({v.split()[0]})")
    if bits:
        sum_lines.append("Special CLIs: " + ", ".join(bits))
sum_lines.append("")
sum_lines.append("Settings & data backed up")
sum_lines.append(f"• config/ total:         {sz(cfg)}")
sum_lines.append(f"• home-overlay:          {sz(ov)}")
sum_lines.append(f"• projects/:             {sz(proj)}")
sum_lines.append(f"• manifests/:            {sz(m)}")
if overlay_present:
    sum_lines.append("• Overlay categories:    " + ", ".join(overlay_present))
if etc_items:
    sum_lines.append(f"• /etc files:            {len(etc_items)} (grub, cups, sddm, …)")
if (cfg / "snap").is_dir() and any((cfg / "snap").iterdir()):
    sum_lines.append(f"• Snap user data:        {sz(cfg / 'snap')}")
if (cfg / "firefox-bookmarks").is_dir():
    sum_lines.append("• Firefox bookmarks:     yes")
if workspaces:
    sum_lines.append(f"• Code workspaces:       {len(workspaces)}")
sum_lines.append("")
sum_lines.append("Full listing written to:")
sum_lines.append("  BACKUP_MANIFEST.md")
sum_lines.append("  manifests/programs-and-clis.txt")
sum_lines.append("  BACKUP_SUMMARY.txt")

summary = "\n".join(sum_lines) + "\n"
(dest / "BACKUP_SUMMARY.txt").write_text(summary, encoding="utf-8")
print(summary, end="")
PY
}

_write_backup_readme() {
  local dest="$1"
  # Full inventory + summary file (stdout discarded here; regenerated for dialog too)
  _generate_backup_inventory "$dest" >/dev/null
}

# Build a human-readable backup summary (also saved to BACKUP_SUMMARY.txt).
_backup_summary_text() {
  local dest="$1"
  # Regenerate so counts match the finished tree, print summary body
  _generate_backup_inventory "$dest"
}

_show_backup_summary() {
  local dest="$1"
  local summary
  summary=$(_backup_summary_text "$dest")
  printf '%s\n' "$summary" > "$dest/BACKUP_SUMMARY.txt"

  # Prefer a scrollable text dialog when available (longer summary).
  if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]]; then
    if _fs_ui text --title "Backup summary" --file "$dest/BACKUP_SUMMARY.txt" --ok-label "Close" 2>/dev/null; then
      return 0
    fi
  fi
  if command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    # Long text: use text-info from the summary file
    zenity --text-info --title="Backup summary" --filename="$dest/BACKUP_SUMMARY.txt" \
      --width=640 --height=560 --ok-label="Close" 2>/dev/null \
      || _fs_msg "$summary"
  else
    printf '%s\n' "$summary"
  fi
}

# ---------------------------------------------------------------------------
# Public: backup
# ---------------------------------------------------------------------------
fedora_setup_backup_to() {
  local parent="$1"
  local dest
  dest="$(_dated_backup_dir "$parent")"
  mkdir -p "$dest" || { _fs_err "Cannot create $dest"; return 1; }
  # A blueprint can contain SSH/GPG private keys and API tokens, so it must never
  # be readable by other local users regardless of the ambient umask.
  chmod 700 "$dest" || { _fs_err "Cannot secure $dest"; return 1; }
  mark_tree_incomplete "$dest" "backup"

  # Announce the destination before any work starts. If the caller cancels, this
  # is the only way it can tell the user which folder to clean up.
  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    echo "DEST=$dest"
  fi

  # Persist selected opts into the backup for transparency
  if [[ -n "${URSTACK_BACKUP_OPTS:-}" ]]; then
    mkdir -p "$dest/manifests" 2>/dev/null || true
    printf '%s\n' "$URSTACK_BACKUP_OPTS" > "$dest/manifests/.include-opts" 2>/dev/null || true
  fi

  _run_backup_steps() {
    if [[ "$(_backup_include manifests 1)" == "1" ]]; then
      echo "0"; _fs_progress "Collecting programs, CLIs & package manifests..."
      _backup_manifests "$dest"
    else
      mkdir -p "$dest/manifests"
      echo "0"; _fs_progress "Skipping package manifests..."
    fi
    if [[ "$(_backup_include appimages 1)" == "1" ]]; then
      echo "20"; _fs_progress "Archiving AppImages & vendor launchers..."
      _backup_appimages_and_vendor "$dest"
    else
      echo "20"; _fs_progress "Skipping AppImages..."
    fi
    if [[ "$(_backup_include projects 1)" == "1" ]]; then
      echo "35"; _fs_progress "Archiving project repositories..."
      _backup_projects "$dest"
    else
      echo "35"; _fs_progress "Skipping projects..."
    fi
    echo "50"; _fs_progress "Archiving custom paths..."
    _backup_extra_user_paths "$dest"
    if [[ "$(_backup_include settings 1)" == "1" \
       || "$(_backup_include secrets 1)" == "1" \
       || "$(_backup_include browsers 1)" == "1" \
       || "$(_backup_include full_dotconfig 1)" == "1" \
       || "$(_backup_include system 1)" == "1" ]]; then
      echo "60"; _fs_progress "Archiving settings, themes, secrets..."
      _backup_config "$dest"
    else
      echo "60"; _fs_progress "Skipping settings overlay..."
    fi
    echo "90"; _fs_progress "Writing manifest..."
    _write_backup_readme "$dest"
    echo "95"; _fs_progress "Checksumming blueprint..."
    _write_backup_manifest "$dest" \
      || echo "# WARNING: could not write $_MANIFEST_NAME — restore cannot verify this blueprint"
    echo "100"; _fs_progress "Done."
  }

  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    # Caller (UrStack shell) owns the progress UI — only emit zenity-compatible lines.
    _run_backup_steps
  elif declare -F pipe_to_progress &>/dev/null; then
    ( _run_backup_steps ) | pipe_to_progress "UrStack — Backup" 0
  elif command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    ( _run_backup_steps ) | zenity --progress --title="UrStack Backup" --percentage=0 \
        --width=720 --height=220 --text="Starting backup..." --auto-close --no-cancel 2>/dev/null || true
  else
    echo "Backing up to $dest ..."
    _run_backup_steps >/dev/null
  fi

  # Every step ran, so the tree is a whole blueprint now.
  mark_tree_complete "$dest"

  # Remember last successful backup for Overview / restore hints
  mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/urstack" 2>/dev/null || true
  {
    echo "dest=$dest"
    echo "parent=$(dirname -- "$dest")"
    echo "name=$(basename -- "$dest")"
    echo "created=$(date -Iseconds)"
  } > "${XDG_CONFIG_HOME:-$HOME/.config}/urstack/last-backup.conf" 2>/dev/null || true

  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    echo "# Backup complete"
    echo "100"
    echo "DEST=$dest"
  else
    _show_backup_summary "$dest"
  fi
  echo "$dest"
}

fedora_setup_backup_ui() {
  local parent
  parent="$(_pick_directory "Choose folder to store the backup" "${HOME}/Backups")"
  [[ -n "$parent" ]] || return 0
  mkdir -p "$parent" 2>/dev/null || true
  local dest preview
  dest="$(_dated_backup_dir "$parent")"
  if ! _fs_ask "Create backup at:\n\n$dest\n\nContinue?"; then
    return 0
  fi
  fedora_setup_backup_to "$parent" >/dev/null
}

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
_restore_repos() {
  local dest="$1"
  [[ -d "$dest/manifests/dnf-repos" ]] || return 0
  # No `|| true`: the caller reports OK/failed based on this return value.
  # priv.sh refuses any repo without gpgcheck=1 and a gpgkey, because a restored
  # repo installs packages whose %post scriptlets run as root.
  _priv_restore restore_etc_tree yum_repos "$(_b64 "$dest/manifests/dnf-repos")"
}

_restore_driver_packages() {
  # args: one package name per argument
  local -a pkgs=() rejected=()
  local p
  for p in "$@"; do
    if _valid_pkg_name "$p"; then
      pkgs+=("$p")
    else
      rejected+=("$p")
    fi
  done
  if [[ ${#rejected[@]} -gt 0 ]]; then
    echo "# Ignoring invalid driver package names: ${rejected[*]}" >&2
    _restore_fail "Driver packages (rejected ${#rejected[@]} invalid name(s))"
  fi
  [[ ${#pkgs[@]} -gt 0 ]] || return 0
  local list
  list=$(mktemp)
  printf '%s\n' "${pkgs[@]}" > "$list"
  _priv_restore dnf_install_list drivers "$(_b64 "$list")"
  local ec=$?
  rm -f "$list"
  return "$ec"
}

_filter_grub_for_nvidia() {
  local src_grub="$1" want_nvidia="$2" out="$3"
  if [[ ! -f "$src_grub" ]]; then
    return 0
  fi
  if [[ "$want_nvidia" == "1" ]]; then
    cp -a "$src_grub" "$out"
    # ensure nvidia modeset present
    if ! grep -q 'nvidia-drm.modeset=1' "$out" 2>/dev/null; then
      sed -i 's/^\(GRUB_CMDLINE_LINUX="[^"]*\)"/\1 rd.driver.blacklist=nouveau modprobe.blacklist=nouveau nvidia-drm.modeset=1"/' "$out" 2>/dev/null || true
    fi
  else
    # strip nvidia/nouveau blacklist bits
    sed -E 's/rd\.driver\.blacklist=nouveau[[:space:]]*//;s/modprobe\.blacklist=nouveau[[:space:]]*//;s/nvidia-drm\.modeset=1[[:space:]]*//;s/initcall_blacklist=nouveau_init[[:space:]]*//' \
      "$src_grub" > "$out" 2>/dev/null || cp -a "$src_grub" "$out"
  fi
}

# ---------------------------------------------------------------------------
# Restore: programs & CLIs from backup manifests
# ---------------------------------------------------------------------------
_restore_ensure_nvm() {
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  if [[ ! -f "$nvm_dir/nvm.sh" ]]; then
    echo "# Installing nvm..."
    curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash 2>&1 || true
  fi
  # shellcheck source=/dev/null
  [[ -f "$nvm_dir/nvm.sh" ]] && . "$nvm_dir/nvm.sh" 2>/dev/null || true
}

_restore_ensure_rustup() {
  if ! command -v rustup &>/dev/null; then
    echo "# Installing rustup..."
    curl -fsSL https://sh.rustup.rs | sh -s -- -y 2>&1 || true
    # shellcheck source=/dev/null
    [[ -f "$HOME/.cargo/env" ]] && . "$HOME/.cargo/env" 2>/dev/null || true
  fi
}

_restore_programs_and_clis() {
  local dest="$1"
  local m="$dest/manifests"

  _fs_progress "Node / nvm + npm packages..."
  if [[ -f "$m/nvm.txt" ]] || [[ -s "$m/npm-global-packages.txt" ]] || [[ -s "$m/npm-user-packages.txt" ]]; then
    _restore_ensure_nvm
    local node_ver=""
    node_ver=$(grep -E '^default=' "$m/nvm.txt" 2>/dev/null | cut -d= -f2-) || true
    [[ -z "$node_ver" ]] && node_ver=$(grep -E '^node=' "$m/nvm.txt" 2>/dev/null | cut -d= -f2- | tr -d 'v') || true
    if command -v nvm &>/dev/null || type nvm &>/dev/null 2>&1; then
      if [[ -n "$node_ver" ]]; then
        nvm install "$node_ver" 2>&1 || nvm install node 2>&1 || true
        nvm alias default "$node_ver" 2>&1 || nvm alias default node 2>&1 || true
      else
        nvm install node 2>&1 || true
        nvm alias default node 2>&1 || true
      fi
    fi
    if command -v npm &>/dev/null; then
      if [[ -s "$m/npm-global-packages.txt" ]]; then
        # Prefer known scoped packages when bare name matches common CLIs
        local pkg
        while IFS= read -r pkg; do
          [[ -z "$pkg" || "$pkg" == npm ]] && continue
          npm install -g "$pkg" 2>&1 || true
        done < "$m/npm-global-packages.txt"
      fi
      if [[ -s "$m/npm-user-packages.txt" ]]; then
        mkdir -p "$HOME/.local"
        local pkg
        while IFS= read -r pkg; do
          [[ -z "$pkg" || "$pkg" == npm ]] && continue
          npm install -g --prefix "$HOME/.local" "$pkg" 2>&1 || true
        done < "$m/npm-user-packages.txt"
      fi
    fi
  fi

  _fs_progress "pip / pipx packages..."
  if [[ -f "$m/pip-user.txt" && -s "$m/pip-user.txt" ]]; then
    python3 -m pip install --user -r "$m/pip-user.txt" 2>&1 || true
  fi
  if [[ -s "$m/pipx-packages.txt" ]]; then
    if ! command -v pipx &>/dev/null; then
      python3 -m pip install --user pipx 2>&1 || true
      python3 -m pipx ensurepath 2>&1 || true
    fi
    if command -v pipx &>/dev/null; then
      local px
      while IFS= read -r px; do
        [[ -z "$px" ]] && continue
        pipx install "$px" 2>&1 || pipx upgrade "$px" 2>&1 || true
      done < "$m/pipx-packages.txt"
    fi
  fi

  _fs_progress "Rustup / Cargo crates..."
  if [[ -s "$m/cargo-crates.txt" ]] || [[ -f "$m/rustup.txt" ]]; then
    _restore_ensure_rustup
    if command -v rustup &>/dev/null; then
      rustup default stable 2>&1 || true
      # Reinstall toolchains listed in backup when present
      if [[ -f "$m/rustup-toolchains.txt" ]]; then
        local tc
        while IFS= read -r tc; do
          [[ -z "$tc" || "$tc" == *"no installed"* ]] && continue
          # lines like: stable-x86_64-unknown-linux-gnu (default)
          tc="${tc%% *}"
          [[ -n "$tc" ]] && rustup toolchain install "$tc" 2>&1 || true
        done < "$m/rustup-toolchains.txt"
      fi
    fi
    if command -v cargo &>/dev/null && [[ -s "$m/cargo-crates.txt" ]]; then
      local crate
      while IFS= read -r crate; do
        [[ -z "$crate" ]] && continue
        # cargo-update installs as cargo-update; skip toolchain meta names
        case "$crate" in
          cargo|rustc|rustup|clippy|rustfmt) continue ;;
        esac
        cargo install "$crate" 2>&1 || true
      done < "$m/cargo-crates.txt"
    fi
  fi

  _fs_progress "Special CLIs (claude / supabase / railway)..."
  mkdir -p "$HOME/.local/bin"
  # Claude Code
  if [[ -f "$m/special.json" ]] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("claude") else 1)' "$m/special.json" 2>/dev/null; then
    if command -v npm &>/dev/null; then
      npm install -g @anthropic-ai/claude-code 2>&1 || true
    fi
    # Official installer fallback
    if ! command -v claude &>/dev/null; then
      curl -fsSL https://claude.ai/install.sh | bash 2>&1 || true
    fi
  fi
  # Supabase CLI
  if [[ -f "$m/special.json" ]] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("supabase") else 1)' "$m/special.json" 2>/dev/null; then
    local sb_tmp sb_dir sb_asset
    sb_asset="$(supabase_release_asset)"
    sb_tmp=$(mktemp)
    sb_dir=$(mktemp -d)
    if curl -fsSL -o "$sb_tmp" \
        "https://github.com/supabase/cli/releases/latest/download/${sb_asset}" \
      && tar -xzf "$sb_tmp" -C "$sb_dir" \
      && install -m 755 "$sb_dir/supabase" "$HOME/.local/bin/supabase"; then
      echo "# Installed supabase $("$HOME/.local/bin/supabase" --version 2>/dev/null | head -1)"
    else
      echo "# Supabase CLI install failed"
      _restore_fail "Supabase CLI"
    fi
    rm -rf "$sb_tmp" "$sb_dir"
  fi
  # Railway CLI
  if grep -qE '^railway(\s|$)' "$m/cli-on-path.txt" 2>/dev/null \
     || grep -qx 'railway' "$m/local-bin.txt" 2>/dev/null \
     || python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("railway") else 1)' "$m/special.json" 2>/dev/null; then
    if command -v npm &>/dev/null; then
      npm install -g @railway/cli 2>&1 || true
    fi
    if ! command -v railway &>/dev/null; then
      curl -fsSL https://railway.com/install.sh | sh 2>&1 || true
    fi
  fi

  _fs_progress "Cursor extensions..."
  if command -v cursor &>/dev/null && [[ -s "$m/cursor-extensions.txt" ]]; then
    local ext
    while IFS= read -r ext; do
      [[ -z "$ext" ]] && continue
      cursor --install-extension "$ext" 2>&1 || true
    done < "$m/cursor-extensions.txt"
  fi
}

fedora_setup_restore_from() {
  local dest="$1"
  [[ -d "$dest/manifests" ]] || { _fs_err "Not a valid backup:\n$dest"; return 1; }

  # A cancelled or interrupted backup is a partial tree. Restoring one would
  # apply whichever steps happened to finish and silently skip the rest.
  if tree_is_incomplete "$dest"; then
    _fs_err "This backup never finished, so it is missing an unknown number of steps."$'\n\n'"$dest"$'\n\n'"Refusing to restore it. Run a new backup, or restore a different one."
    return 1
  fi

  # Integrity gate. A blueprint drives root package installs and /etc writes, so
  # a tampered one is a root code-execution path — check before trusting any of it.
  local vreport vrc=0 vpriv=0 vother=0 vline vpath
  vreport=$(mktemp)
  _verify_backup_manifest "$dest" "$vreport" || vrc=$?
  if [[ $vrc -eq 2 ]]; then
    if ! _fs_ask "This blueprint has no integrity manifest, so it cannot be checked for tampering."$'\n\n'"That is expected for one made by an older UrStack. Restore it anyway?"; then
      rm -f "$vreport"; return 0
    fi
  elif [[ $vrc -ne 0 ]]; then
    while IFS= read -r vline; do
      [[ -n "$vline" ]] || continue
      vpath="${vline%%:*}"
      if _manifest_path_is_privileged "$vpath"; then vpriv=$((vpriv + 1)); else vother=$((vother + 1)); fi
    done < "$vreport"
    if [[ $vpriv -gt 0 ]]; then
      _fs_err "This blueprint has been modified since it was created."$'\n\n'"$vpriv file(s) that a restore would hand to root do not match their checksums — package lists, repository files or /etc content."$'\n\n'"Refusing to restore. Details:"$'\n'"$(head -20 "$vreport")"
      rm -f "$vreport"
      return 1
    fi
    if ! _fs_ask "This blueprint has been modified since it was created."$'\n\n'"$vother file(s) of your own data do not match their checksums. Nothing that runs as root is affected."$'\n\n'"Restore anyway?"; then
      rm -f "$vreport"; return 0
    fi
  fi
  rm -f "$vreport"

  local bak_ver cur_ver
  bak_ver=$(tr -d '[:space:]' < "$dest/manifests/fedora-release.txt" 2>/dev/null || true)
  cur_ver=$(rpm -E %fedora 2>/dev/null || true)
  if [[ -n "$bak_ver" && -n "$cur_ver" && "$bak_ver" != "$cur_ver" ]]; then
    if ! _fs_ask "This backup is from Fedora ${bak_ver}; this machine is Fedora ${cur_ver}."$'\n\n'"Package restore may fail or mix releases. Continue anyway?"; then
      return 0
    fi
  fi

  local same_hw=1
  local backup_hw="$dest/manifests/hw-inventory.txt"
  local tmp_hw
  tmp_hw=$(mktemp)
  _write_hw_inventory "$tmp_hw"

  # Auto-detect different GPU vendors
  local src_n=0 tgt_n=0
  grep -q "\[${_PCI_NVIDIA}:" "$backup_hw" 2>/dev/null && src_n=1
  _detect_gpus
  tgt_n=$HAS_NVIDIA
  if [[ $src_n -ne $tgt_n ]] || \
     { grep -q "\[${_PCI_AMD}:" "$backup_hw" 2>/dev/null; [[ $HAS_AMD -eq 0 ]]; } || \
     { grep -q "\[${_PCI_INTEL}:" "$backup_hw" 2>/dev/null; [[ $HAS_INTEL_GPU -eq 0 && $HAS_NVIDIA -eq 0 && $HAS_AMD -eq 0 ]]; }; then
    same_hw=0
  fi
  # Simpler auto: compare nvidia presence
  [[ $src_n -eq $tgt_n ]] || same_hw=0

  local hw_choice="same"
  local suggest
  suggest=$([[ $same_hw -eq 1 ]] && echo same || echo different)

  local do_drivers=0 do_packages=0
  [[ "$(_backup_include drivers 1)" == "1" ]] && do_drivers=1
  [[ "$(_backup_include packages 1)" == "1" ]] && do_packages=1

  if [[ $do_drivers -eq 1 ]]; then
    if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]]; then
      local items_f def_same="TRUE" def_diff="FALSE"
      [[ $same_hw -eq 0 ]] && def_same="FALSE" && def_diff="TRUE"
      items_f=$(mktemp)
      printf '%s\n' \
        "$def_same|same|Same hardware — restore drivers as backed up" \
        "$def_diff|different|Different hardware — only install matching / needed drivers" \
        > "$items_f"
      hw_choice=$(_fs_ui radio --title "Hardware" \
        --text "Is this the same PC as the backup, or different hardware? (Auto-detect suggests: $suggest)" \
        --items-file "$items_f" --ok-label "Continue") || hw_choice=""
      rm -f "$items_f"
      case "$hw_choice" in
        different) same_hw=0 ;;
        same) same_hw=1 ;;
        *) return 0 ;; # cancelled
      esac
    elif command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
      local def_same="TRUE" def_diff="FALSE"
      [[ $same_hw -eq 0 ]] && def_same="FALSE" && def_diff="TRUE"
      hw_choice=$(zenity --list --radiolist --title="Hardware" \
        --text="Is this the same PC as the backup, or different hardware?\n(Auto-detect suggests: $suggest)" \
        --column="" --column="Option" \
        "$def_same" "Same hardware" \
        "$def_diff" "Different hardware" \
        --height=280 --width=420 2>/dev/null) || true
      case "$hw_choice" in
        "Different hardware") same_hw=0 ;;
        *) same_hw=1 ;;
      esac
    else
      if [[ $same_hw -eq 0 ]]; then
        echo "Different hardware detected (or assumed)."
        same_hw=0
      fi
    fi
  fi

  # Build package install set from drivers
  local install_pkgs=()
  local want_nvidia=0
  local line status gid label pkgs
  local proposals
  proposals=$(mktemp)

  if [[ $do_drivers -eq 1 ]]; then
    if [[ $same_hw -eq 1 ]]; then
      _propose_target_driver_groups "$dest/manifests/drivers.json" 2>/dev/null | grep '^matched|' > "$proposals" || true
      # force all backup groups that match + always
      _propose_target_driver_groups "$dest/manifests/drivers.json" > "$proposals" 2>/dev/null || true
    else
      _propose_target_driver_groups "$dest/manifests/drivers.json" > "$proposals" 2>/dev/null || true
    fi

    local checklist=()
    local items_f=""
    items_f=$(mktemp)
    while IFS='|' read -r status gid label pkgs; do
      [[ -n "$gid" ]] || continue
      local on="TRUE"
      [[ "$status" == "skipped" ]] && on="FALSE"
      [[ $same_hw -eq 1 && "$status" == "skipped" ]] && continue
      checklist+=("$on" "$status:$gid" "$label")
      printf '%s|%s|%s\n' "$on" "$status:$gid" "$label" >> "$items_f"
    done < "$proposals"

    local selected=""
    local drivers_cancelled=0
    if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]] && [[ -s "$items_f" ]]; then
      if ! selected=$(_fs_ui checklist --title "Drivers" \
        --text "Select driver groups to install:" \
        --items-file "$items_f" --ok-label "Install selected"); then
        drivers_cancelled=1
        selected=""
      fi
    elif command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]] && [[ ${#checklist[@]} -gt 0 ]]; then
      selected=$(zenity --list --checklist --title="Drivers" \
        --text="Select driver groups to install:" \
        --column="" --column="id" --column="Description" --hide-column=2 \
        --print-column=2 --separator="|" \
        "${checklist[@]}" --width=640 --height=400 2>/dev/null) || drivers_cancelled=1
    else
      # default: all matched+new
      selected=$(awk -F'|' '$1=="matched"||$1=="new"{print $1":"$2}' "$proposals" | paste -sd'|' -)
    fi
    rm -f "$items_f"
    if [[ $drivers_cancelled -eq 1 ]]; then
      rm -f "$proposals" "$tmp_hw"
      return 0
    fi

    local sel
    IFS='|' read -ra _sels <<< "${selected:-}"
    for sel in "${_sels[@]:-}"; do
      [[ -n "$sel" ]] || continue
      gid="${sel#*:}"
      pkgs=$(awk -F'|' -v g="$gid" '$2==g{print $4; exit}' "$proposals")
      [[ "$gid" == "nvidia" ]] && want_nvidia=1
      if [[ -n "$pkgs" ]]; then
        IFS=',' read -ra _ps <<< "$pkgs"
        install_pkgs+=("${_ps[@]}")
      fi
    done
  fi
  rm -f "$proposals" "$tmp_hw"

  # Progress restore
  local logf report_path failc
  logf=$(mktemp)
  _restore_track_init
  _restore_init_cancel_undo || true
  {
    echo "=== Restore $(date -Iseconds) ==="
    echo "Include opts: ${URSTACK_BACKUP_OPTS:-(defaults)}"

    if [[ $do_packages -eq 1 ]]; then
      _fs_progress "Installing repositories..."
      if _restore_repos "$dest"; then _restore_ok "DNF repositories"; else _restore_fail "DNF repositories"; fi
    else
      _restore_skip "DNF repositories (disabled)"
    fi

    if [[ $do_drivers -eq 1 ]]; then
      _fs_progress "Installing drivers..."
      if [[ ${#install_pkgs[@]} -gt 0 ]]; then
        if _restore_driver_packages "${install_pkgs[@]}"; then
          _restore_ok "Driver packages"
        else
          _restore_fail "Driver packages"
        fi
      else
        _restore_skip "Driver packages (none selected)"
      fi
    else
      _restore_skip "Driver packages (disabled)"
    fi

    if [[ $do_packages -eq 1 ]]; then
      _fs_progress "Installing DNF user packages (filtered)..."
      local pkgfile="$dest/manifests/dnf-user-packages.txt"
      if [[ -f "$pkgfile" ]]; then
        local filtered pkg_rejected=0 _pkg
        filtered=$(mktemp)
        while IFS= read -r _pkg; do
          _pkg="${_pkg%%[[:space:]]*}"
          [[ -z "$_pkg" || "$_pkg" == \#* ]] && continue
          [[ $want_nvidia -eq 0 && "$_pkg" == *nvidia* ]] && continue
          if _valid_pkg_name "$_pkg"; then
            printf '%s\n' "$_pkg" >> "$filtered"
          else
            pkg_rejected=$((pkg_rejected + 1))
            echo "# Ignoring invalid package name: $_pkg" >&2
          fi
        done < "$pkgfile"
        if [[ $pkg_rejected -gt 0 ]]; then
          _restore_fail "DNF user packages (rejected $pkg_rejected invalid name(s))"
        fi
        if [[ -s "$filtered" ]]; then
          if _priv_restore dnf_install_list user "$(_b64 "$filtered")"; then
            _restore_ok "DNF user packages"
          else
            _restore_fail "DNF user packages (some may have failed)"
          fi
        else
          _restore_skip "DNF user packages (empty after filter)"
        fi
        rm -f "$filtered"
      else
        _restore_skip "DNF user packages (no manifest)"
      fi
    else
      _restore_skip "DNF user packages (disabled)"
    fi

    if [[ "$(_backup_include system 1)" == "1" ]]; then
      _fs_progress "Restoring groups..."
      if [[ -f "$dest/manifests/user-groups.txt" ]]; then
        # $USER is unset when launched from a .desktop file or a user unit.
        local me primary g
        me=$(id -un)
        primary=$(id -gn "$me" 2>/dev/null)
        local -a add=() skipped=() privileged=()
        for g in $(tr ',' ' ' < "$dest/manifests/user-groups.txt"); do
          [[ -z "$g" || "$g" == "$primary" ]] && continue
          if ! getent group "$g" >/dev/null 2>&1; then
            skipped+=("$g"); continue
          fi
          id -nG "$me" 2>/dev/null | tr ' ' '\n' | grep -qx "$g" && continue
          # wheel/docker/lxd are root-equivalent. Restoring an admin's blueprint
          # onto a standard account must not quietly hand over the machine.
          case "$g" in
            wheel|sudo|docker|lxd|libvirt|kvm) privileged+=("$g"); continue ;;
          esac
          add+=("$g")
        done
        [[ ${#skipped[@]} -gt 0 ]] && echo "# Groups not present on this system: ${skipped[*]}"
        if [[ ${#privileged[@]} -gt 0 ]]; then
          echo "# Skipped privilege-granting groups: ${privileged[*]}"
          echo "#   add them yourself if intended: sudo usermod -aG ${privileged[*]// /,} $me"
          _restore_skip "Privileged groups (${privileged[*]})"
        fi
        if [[ ${#add[@]} -eq 0 ]]; then
          _restore_skip "User groups (nothing to add)"
        else
          # One at a time: usermod is all-or-nothing, so a single bad entry would
          # otherwise silently drop every group.
          if _priv_restore usermod_add_groups "$me" "$(IFS=,; echo "${add[*]}")"; then
            _restore_ok "User groups (${add[*]})"
          else
            _restore_fail "User groups (one or more failed)"
          fi
        fi
      fi

      _fs_progress "Restoring GRUB / SDDM / sysctl..."
      local grub_tmp
      grub_tmp=$(mktemp)
      _filter_grub_for_nvidia "$dest/config/etc/default/grub" "$want_nvidia" "$grub_tmp"
      if [[ -f "$grub_tmp" && -s "$grub_tmp" ]]; then
        if _priv_restore restore_grub "$(_b64 "$grub_tmp")"; then
          _restore_ok "GRUB"
        else
          _restore_fail "GRUB"
        fi
      fi
      rm -f "$grub_tmp"

      if [[ -d "$dest/manifests/sddm-themes" ]]; then
        if _priv_restore restore_etc_tree sddm_themes "$(_b64 "$dest/manifests/sddm-themes")"; then
          _restore_ok "SDDM themes"
        else
          _restore_fail "SDDM themes"
        fi
      fi
      if [[ -d "$dest/config/etc/sddm.conf.d" ]]; then
        _priv_restore restore_etc_tree sddm_conf "$(_b64 "$dest/config/etc/sddm.conf.d")" \
          && _restore_ok "SDDM config" || _restore_fail "SDDM config"
      fi
      if [[ -d "$dest/config/etc/sysctl.d" ]]; then
        _priv_restore restore_etc_tree sysctl "$(_b64 "$dest/config/etc/sysctl.d")" \
          && _restore_ok "sysctl" || _restore_fail "sysctl"
      fi
      if [[ -d "$dest/config/etc/modules-load.d" ]]; then
        _priv_restore restore_etc_tree modules_load "$(_b64 "$dest/config/etc/modules-load.d")" \
          && _restore_ok "modules-load" || _restore_fail "modules-load"
      fi

      _fs_progress "Locale..."
      if [[ -f "$dest/manifests/system-locale.txt" ]]; then
        local lang keymap
        lang=$(grep -E 'LANG=' "$dest/manifests/system-locale.txt" | head -1 | awk '{print $NF}')
        keymap=$(grep -E 'VC Keymap:' "$dest/manifests/system-locale.txt" | awk '{print $NF}')
        [[ -n "$lang" ]] && _priv_restore set_locale "$lang" && _restore_ok "Locale LANG" || true
        [[ -n "$keymap" && "$keymap" != "n/a" ]] && _priv_restore set_keymap "$keymap" && _restore_ok "Keymap" || true
      fi
    else
      _restore_skip "System config (disabled)"
    fi

    if [[ "$(_backup_include flatpak 1)" == "1" ]]; then
      _fs_progress "Flatpak..."
      if [[ -f "$dest/manifests/flatpak-apps.txt" ]]; then
        local fp_fail=0
        while read -r app origin inst; do
          [[ -z "$app" || "$app" == Application ]] && continue
          local scope="--system"
          [[ "$inst" == *user* ]] && scope="--user"
          flatpak install -y $scope "$origin" "$app" 2>&1 || fp_fail=1
        done < "$dest/manifests/flatpak-apps.txt"
        [[ $fp_fail -eq 0 ]] && _restore_ok "Flatpak apps" || _restore_fail "Flatpak apps (one or more failed)"
      fi
    else
      _restore_skip "Flatpak apps (disabled)"
    fi

    if [[ "$(_backup_include snap 1)" == "1" ]]; then
      _fs_progress "Snap..."
      if [[ -f "$dest/manifests/snap-packages.txt" ]]; then
        local snap_jobs=()
        while read -r name rest; do
          [[ "$name" == Name || -z "$name" || "$name" == "Name" ]] && continue
          snap_jobs+=("snap_install $name")
        done < <(tail -n +2 "$dest/manifests/snap-packages.txt")
        if [[ ${#snap_jobs[@]} -eq 0 ]]; then
          _restore_skip "Snap packages (empty)"
        elif _restore_run_priv_batch "${snap_jobs[@]}"; then
          _restore_ok "Snap packages"
        else
          _restore_fail "Snap packages (one or more failed)"
        fi
      fi
      if [[ -d "$dest/config/snap" ]]; then
        mkdir -p "$HOME/snap"
        rsync -a "$dest/config/snap"/ "$HOME/snap"/ 2>&1 && _restore_ok "Snap user data" || _restore_fail "Snap user data"
      fi
    else
      _restore_skip "Snap (disabled)"
    fi

    if [[ "$(_backup_include projects 1)" == "1" ]]; then
      _fs_progress "Projects..."
      if [[ -d "$dest/projects" ]]; then
        _ensure_pre_restore_dir
        rsync -a "${_HOME_RSYNC_OPTS[@]}" "$dest/projects"/ "$HOME"/ 2>&1 \
          && _restore_ok "Project trees" || _restore_fail "Project trees"
      else
        _restore_skip "Project trees (none in backup)"
      fi
      if [[ -d "$dest/extra" ]]; then
        _fs_progress "Custom paths..."
        _ensure_pre_restore_dir
        rsync -a "${_HOME_RSYNC_OPTS[@]}" --exclude='_outside/' "$dest/extra"/ "$HOME"/ 2>&1 \
          && _restore_ok "Custom paths" || _restore_fail "Custom paths"
        if [[ -d "$dest/extra/_outside" ]]; then
          # Outside-$HOME paths are kept for reference; copy next to home as recoverable tree
          mkdir -p "$HOME/UrStack-restored-outside"
          rsync -a "$dest/extra/_outside"/ "$HOME/UrStack-restored-outside"/ 2>&1 \
            && _restore_ok "Custom paths outside home → ~/UrStack-restored-outside" \
            || _restore_fail "Custom paths outside home"
        fi
      fi
    else
      _restore_skip "Project trees (disabled)"
      _restore_skip "Custom paths (disabled with projects)"
    fi

    if [[ "$(_backup_include settings 1)" == "1" ]]; then
      _fs_progress "Home overlay (settings)..."
      if [[ -d "$dest/config/home-overlay" ]]; then
        local -a ov_excludes=()
        if [[ "$(_backup_include secrets 1)" != "1" ]]; then
          ov_excludes+=(
            --exclude='.ssh/'
            --exclude='.gnupg/'
            --exclude='.netrc'
            --exclude='.git-credentials'
            --exclude='.config/gh/'
          )
        fi
        if [[ "$(_backup_include browsers 1)" != "1" ]]; then
          ov_excludes+=(
            --exclude='.config/google-chrome/'
            --exclude='.mozilla/'
          )
        fi
        _ensure_pre_restore_dir
        if rsync -a "${_HOME_RSYNC_OPTS[@]}" "${ov_excludes[@]}" "$dest/config/home-overlay"/ "$HOME"/ 2>&1; then
          _restore_ok "Home settings overlay"
        else
          _restore_fail "Home settings overlay"
        fi
        _repair_secret_modes
      fi
    else
      _restore_skip "Home settings overlay (disabled)"
    fi

    if [[ "$(_backup_include browsers 1)" == "1" ]]; then
      if [[ -f "$dest/config/firefox-bookmarks/places.sqlite" ]]; then
        local ffprofile
        ffprofile=$(find "$HOME/.mozilla/firefox" -maxdepth 1 -type d -name '*.default-release' 2>/dev/null | head -1)
        if [[ -n "$ffprofile" ]]; then
          cp -a "$dest/config/firefox-bookmarks/places.sqlite" "$ffprofile/" 2>/dev/null \
            && _restore_ok "Firefox bookmarks" || _restore_fail "Firefox bookmarks"
          [[ -f "$dest/config/firefox-bookmarks/favicons.sqlite" ]] && \
            cp -a "$dest/config/firefox-bookmarks/favicons.sqlite" "$ffprofile/" 2>/dev/null || true
        else
          _restore_skip "Firefox bookmarks (no profile yet — open Firefox once, then re-copy)"
        fi
      fi
    else
      _restore_skip "Browser bookmarks (disabled)"
    fi

    if [[ "$(_backup_include appimages 1)" == "1" ]]; then
      _fs_progress "AppImages & vendor notes..."
      _restore_appimages_and_vendor "$dest"
    else
      _restore_skip "AppImages (disabled)"
    fi

    if [[ "$(_backup_include system 1)" == "1" ]]; then
      _fs_progress "CUPS / bin scripts..."
      if [[ -d "$dest/config/etc/cups" ]]; then
          _priv_restore restore_etc_tree cups "$(_b64 "$dest/config/etc/cups")" \
          && _restore_ok "CUPS printers" || _restore_fail "CUPS printers"
      fi
      if [[ -d "$dest/manifests/bin-scripts/bin" ]]; then
        mkdir -p "$HOME/bin"
        if rsync -a "$dest/manifests/bin-scripts/bin"/ "$HOME/bin"/ 2>&1; then
          chmod -R u+X "$HOME/bin" 2>/dev/null || true
          find "$HOME/bin" -type f -name '*.sh' -exec chmod +x {} \; 2>/dev/null || true
          [[ -f "$HOME/bin/check-fedora-updates.sh" ]] && chmod +x "$HOME/bin/check-fedora-updates.sh"
          _restore_ok "bin scripts"
        else
          _restore_fail "bin scripts"
        fi
      fi
      if [[ -f "$dest/manifests/bin-scripts/check-fedora-updates.desktop" ]]; then
        mkdir -p "$HOME/.local/share/applications"
        cp -a "$dest/manifests/bin-scripts/check-fedora-updates.desktop" \
          "$HOME/.local/share/applications/" 2>/dev/null || true
      fi
      if [[ -f "$dest/manifests/bin-scripts/immich-go" ]]; then
        cp -a "$dest/manifests/bin-scripts/immich-go" "$HOME/immich-go" && chmod +x "$HOME/immich-go" \
          && _restore_ok "immich-go" || _restore_fail "immich-go"
      fi

      # -s not -f: `crontab` replaces rather than merges, so an empty manifest
      # would wipe whatever this machine already has.
      if [[ -s "$dest/manifests/user-crontab.txt" ]]; then
        crontab -l > "${XDG_STATE_HOME:-$HOME/.local/state}/urstack/crontab-before-restore.txt" 2>/dev/null || true
        crontab "$dest/manifests/user-crontab.txt" 2>&1 && _restore_ok "crontab" || _restore_fail "crontab"
      fi
      systemctl --user daemon-reload 2>&1 || true
    fi

    if [[ "$(_backup_include programs 1)" == "1" ]]; then
      _fs_progress "Programs & CLIs (npm/pip/cargo/…)..."
      if _restore_programs_and_clis "$dest"; then
        _restore_ok "Programs & CLIs"
      else
        _restore_fail "Programs & CLIs (see log)"
      fi
    else
      _restore_skip "Programs & CLIs (disabled)"
    fi

    echo "=== Restore finished ==="
  } 2>&1 | tee "$logf" | if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    # Strip noisy command output; keep # progress markers for the in-app UI
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        \#*|===*) echo "$line" ;;
      esac
    done
  elif declare -F pipe_to_progress &>/dev/null; then
    # Convert free-form restore log into a pulsing GTK progress window
    {
      echo "# Restoring…"
      while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == \#* ]]; then
          echo "$line"
        fi
      done
      echo "# Finishing…"
      echo "100"
    } | pipe_to_progress "UrStack — Restore" 1
  elif command -v zenity &>/dev/null && [[ -n "${DISPLAY:-}" ]]; then
    zenity --progress --pulsate --title="UrStack Restore" --text="Restoring..." \
      --width=720 --height=200 --auto-close --no-cancel 2>/dev/null || cat
  else
    cat
  fi

  report_path=$(_restore_write_report "$dest" "$logf")
  # Keep a copy of the log next to the report
  cp -a "$logf" "$dest/RESTORE_LOG.txt" 2>/dev/null || true
  failc=$(wc -l < "${_RESTORE_FAIL_FILE:-/dev/null}" 2>/dev/null | tr -d ' ')
  _restore_track_cleanup

  local reboot_note=""
  [[ $want_nvidia -eq 1 ]] && reboot_note=$'\n\nReboot recommended for NVIDIA drivers.'
  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    echo "# Restore finished"
    echo "100"
    echo "REPORT=$report_path"
    echo "FAILS=${failc:-0}"
  else
    if [[ -n "${DISPLAY:-}" ]] && command -v python3 &>/dev/null && [[ -x "$_FEDORA_UI" ]] && [[ -f "$report_path" ]]; then
      _fs_ui text --title "Restore report" --file "$report_path" --ok-label "Close" 2>/dev/null || true
    fi
    if [[ "${failc:-0}" != "0" ]]; then
      _fs_err "Restore finished with ${failc} failed step(s).$reboot_note\n\nReport:\n$report_path\nLog:\n$dest/RESTORE_LOG.txt"
    else
      _fs_msg "Restore finished from:\n$dest$reboot_note\n\nReport:\n$report_path\n\nReview vendor-launchers list if any outside-store apps need a manual installer."
    fi
  fi
  rm -f "$logf"
}

fedora_setup_restore_ui() {
  local start="${HOME}/Backups"
  [[ -d "$start" ]] || start="$HOME"
  local dest
  dest="$(_pick_directory "Select a fedora-setup-* backup folder" "$start")"
  [[ -n "$dest" ]] || return 0
  # If user picked parent, try to find latest fedora-setup-*
  if [[ ! -d "$dest/manifests" ]]; then
    local latest
    latest=$(ls -dt "$dest"/fedora-setup-* 2>/dev/null | head -1)
    if [[ -n "$latest" && -d "$latest/manifests" ]]; then
      dest="$latest"
    fi
  fi
  [[ -d "$dest/manifests" ]] || { _fs_err "No manifests/ in:\n$dest"; return 1; }
  _fs_ask "Restore from:\n\n$dest\n\nThis will install packages and overwrite settings. Continue?" || return 0
  fedora_setup_restore_from "$dest"
}
