#!/usr/bin/env bash
# Privileged helper for Fedora Updates — intended to run once via pkexec.
# Reads a jobs file (one job per line) and executes in order.
#
# Job lines:
#   ensure_cursor_repo
#   ensure_discover_exclude
#   dnf_upgrade
#   prune_old_kernels
#   snap_refresh
#   fwupd_update
#   pip_build_deps
#   cursor_rpm </abs/path.rpm>            (alias of install_local_rpm)
#   install_local_rpm </abs/path.rpm>
#   cursor_dnf
#   akmods_wait
#   dnf_clean_all
#   dnf_autoremove
#   journal_vacuum
#   rpmfusion_enable
#   codecs_install
#   zram_enable
#   earlyoom_enable
#   ppd_enable
#   tlp_disable
#   fstrim_all
#   coredump_vacuum
#   snap_purge_old
#   dnf_speed_conf
#   sysctl_urstack
#   health_restore_files </abs/restore-point-dir>
#   dnf_history_rollback <transaction-id>
#   unit_set_enabled <unit> <enabled|disabled|static|...>
#   restore_etc_tree <yum_repos|sddm_themes|sddm_conf|sysctl|modules_load|cups> <base64 abs src dir>
#   restore_grub <base64 abs path to a grub default file>
#   ensure_snapd
#   snap_install <name>
#   dnf_install_list <label> <base64 caller-owned package list>
#   dnf_install_pkg <name>
#   usermod_add_groups <caller-username> <g1,g2,...>
#   set_locale <LANG or en_GB.UTF-8>
#   set_keymap <gb>
#   restore_session_unwind
# Path arguments to the restore verbs are base64 so paths containing spaces
# survive the whitespace-split job protocol.
#
# Env:
#   TIMEOUT_AKMODS, KEEP_KERNELS, HEALTH_JOURNAL_VACUUM,
#   FEDORA_UPDATES_AKMODS_CANCEL (path — if created, abort akmods)
#   URSTACK_RESTORE_CANCEL (path — if created, skip remaining restore jobs and unwind)
#   URSTACK_RESTORE_UNDO (caller-owned journal dir under ~/.local/state/stackup/)

set -uo pipefail

JOBS_FILE="${1:-}"
[[ -n "$JOBS_FILE" && -f "$JOBS_FILE" ]] || { echo "usage: $0 <jobs-file>" >&2; exit 2; }

TIMEOUT_AKMODS="${TIMEOUT_AKMODS:-600}"
CANCEL_FLAG="${FEDORA_UPDATES_AKMODS_CANCEL:-}"

log() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 2; }

# ── Input validation ─────────────────────────────────────────────────────────
# This script runs as root via pkexec but is handed a jobs file written by an
# unprivileged caller. Everything the caller controls is treated as hostile:
# the jobs file itself, and every job argument that reaches dnf/systemctl/cp.

# pkexec exports the calling user's uid; sudo exports SUDO_UID. With neither we
# were run directly, so the current uid is the caller.
if [[ -n "${PKEXEC_UID:-}" ]]; then
  CALLER_UID="$PKEXEC_UID"
elif [[ -n "${SUDO_UID:-}" ]]; then
  CALLER_UID="$SUDO_UID"
else
  CALLER_UID="$(id -u)"
fi
[[ "$CALLER_UID" =~ ^[0-9]+$ ]] || die "priv: bad caller uid"

CALLER_HOME=$(getent passwd "$CALLER_UID" 2>/dev/null | cut -d: -f6)
[[ -n "$CALLER_HOME" ]] || CALLER_HOME="/root"

# Reject symlinks, foreign owners, and anything the group or world can write —
# otherwise another process could swap the instruction list out from under us.
_validate_jobs_file() {
  local f="$1" owner perms
  [[ -L "$f" ]] && die "priv: jobs file is a symlink — refusing"
  owner=$(stat -c '%u' "$f" 2>/dev/null) || die "priv: cannot stat jobs file"
  perms=$(stat -c '%a' "$f" 2>/dev/null) || die "priv: cannot stat jobs file"
  if [[ "$owner" != "$CALLER_UID" && "$owner" != "0" ]]; then
    die "priv: jobs file owned by uid $owner, expected $CALLER_UID — refusing"
  fi
  if (( 8#$perms & 022 )); then
    die "priv: jobs file is group/world writable (mode $perms) — refusing"
  fi
}

# A bare systemd unit name. Rejecting '/' is the point: `systemctl enable` will
# happily link and start a unit file given by absolute path.
_valid_unit_name() {
  [[ "$1" =~ ^[A-Za-z0-9@:._\\-]+\.(service|socket|timer|target|path|mount|slice|scope)$ ]]
}


_valid_pkg_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]
}

# Snap names from a backup become `snap install` as root.
_valid_snap_name() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]*$ ]] && (( ${#1} <= 40 ))
}

_ensure_snapd_bin() {
  if [[ -x /usr/bin/snap ]]; then
    [[ -e /snap ]] || ln -sfn /var/lib/snapd/snap /snap
    systemctl enable --now snapd.socket >/dev/null 2>&1 || true
    return 0
  fi
  log "Installing snapd…"
  dnf install -y --skip-broken --skip-unavailable snapd || return 1
  mkdir -p /var/lib/snapd/snap
  ln -sfn /var/lib/snapd/snap /snap
  systemctl enable --now snapd.socket >/dev/null 2>&1 || true
  systemctl start snapd.service >/dev/null 2>&1 || true
  local i
  for ((i=1; i<=45; i++)); do
    [[ -x /usr/bin/snap ]] && return 0
    sleep 1
  done
  log "ensure_snapd: /usr/bin/snap still missing"
  return 1
}

# Absolute, traversal-free path owned by the caller and not a symlink.
_valid_caller_path() {
  local p="$1" owner
  [[ "$p" == /* ]] || return 1
  [[ "$p" == *".."* ]] && return 1
  [[ -L "$p" ]] && return 1
  [[ -e "$p" ]] || return 1
  owner=$(stat -c '%u' "$p" 2>/dev/null) || return 1
  [[ "$owner" == "$CALLER_UID" || "$owner" == "0" ]]
}

# Job lines are split on whitespace, which cannot express a backup sitting on a
# removable drive named "John's Drive". Path arguments to the restore verbs are
# therefore base64 so they survive the protocol intact; every safety check still
# runs against the decoded value.
_b64_path() {
  local v
  [[ "$1" =~ ^[A-Za-z0-9+/=]+$ ]] || return 1
  v=$(printf '%s' "$1" | base64 -d 2>/dev/null) || return 1
  # An unencoded path is itself valid base64 charset and decodes to binary, so
  # require the result to be printable rather than logging raw bytes.
  [[ -n "$v" ]] || return 1
  [[ "$v" == *[![:print:]]* ]] && return 1
  printf '%s' "$v"
}

# Stricter than _valid_caller_path: the source must belong to the caller, not
# merely be root-owned. A restore copies whatever it is pointed at into /etc as
# world-readable, so accepting a root-owned source would let `/root` be published.
_valid_caller_owned_path() {
  local p="$1" owner
  [[ "$p" == /* ]] || return 1
  [[ "$p" == *".."* ]] && return 1
  [[ -L "$p" ]] && return 1
  [[ -e "$p" ]] || return 1
  owner=$(stat -c '%u' "$p" 2>/dev/null) || return 1
  [[ "$owner" == "$CALLER_UID" ]]
}

# Destinations a restore is allowed to write to. The caller names a key and the
# path is looked up here, so no caller-supplied string ever becomes a target.
_restore_dest_for() {
  case "$1" in
    yum_repos)    printf '%s' /etc/yum.repos.d ;;
    sddm_themes)  printf '%s' /usr/share/sddm/themes ;;
    sddm_conf)    printf '%s' /etc/sddm.conf.d ;;
    sysctl)       printf '%s' /etc/sysctl.d ;;
    modules_load) printf '%s' /etc/modules-load.d ;;
    cups)         printf '%s' /etc/cups ;;
    *) return 1 ;;
  esac
}

_restore_cancel_requested() {
  [[ -n "${URSTACK_RESTORE_CANCEL:-}" && -e "$URSTACK_RESTORE_CANCEL" ]]
}

_valid_undo_dir() {
  local p="${URSTACK_RESTORE_UNDO:-}"
  [[ -n "$p" && "$p" == /* && "$p" != *".."* ]] || return 1
  [[ -d "$p" && ! -L "$p" ]] || return 1
  [[ "$p" == "$CALLER_HOME/.local/state/stackup/"* ]] || return 1
  _valid_caller_owned_path "$p"
}

_dnf_last_history_id() {
  dnf history list 2>/dev/null | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print $1; exit}'
}

_undo_note_dnf_before() {
  local f id
  _valid_undo_dir || return 0
  f="$URSTACK_RESTORE_UNDO/dnf-history-before"
  [[ -f "$f" ]] && return 0
  id=$(_dnf_last_history_id)
  [[ -n "$id" ]] || return 0
  printf '%s\n' "$id" > "$f"
}

# Remember a dest file we are about to overwrite ($1 dest path, $2 dkey, $3 relative name).
_undo_note_overwrite() {
  local dst="$1" dkey="$2" rel="$3" und
  _valid_undo_dir || return 0
  [[ "$rel" != *".."* && "$rel" != /* && -n "$rel" ]] || return 0
  if [[ -f "$dst" && ! -L "$dst" ]]; then
    und="$URSTACK_RESTORE_UNDO/replaced/$dkey/$rel"
    mkdir -p "$(dirname "$und")"
    cp -a "$dst" "$und"
  else
    printf '%s\n' "$dkey/$rel" >> "$URSTACK_RESTORE_UNDO/added-files.txt"
  fi
}

_undo_note_locale_before() {
  local loc km
  _valid_undo_dir || return 0
  [[ -f "$URSTACK_RESTORE_UNDO/locale-before" ]] && return 0
  loc=$(localectl status 2>/dev/null | awk '/System Locale:/{print $NF; exit}')
  km=$(localectl status 2>/dev/null | awk '/VC Keymap:/{print $NF; exit}')
  loc="${loc#LANG=}"
  printf '%s\n' "$loc" > "$URSTACK_RESTORE_UNDO/locale-before"
  printf '%s\n' "$km" > "$URSTACK_RESTORE_UNDO/keymap-before"
}

_priv_unwind_restore_session() {
  log "=== priv: reverting restore (cancelled) ==="
  local hid g uname sname dkey dst f rel rest loc km
  _valid_undo_dir || { log "no undo journal"; log "#unwound fail"; return 1; }
  hid=$(tr -d '[:space:]' < "$URSTACK_RESTORE_UNDO/dnf-history-before" 2>/dev/null || true)
  if [[ "$hid" =~ ^[0-9]+$ ]]; then
    log "dnf history rollback $hid"
    dnf history rollback -y "$hid" || log "dnf rollback reported errors"
  fi
  if [[ -f "$URSTACK_RESTORE_UNDO/snaps-installed.txt" ]]; then
    while IFS= read -r sname || [[ -n "$sname" ]]; do
      [[ -n "$sname" ]] || continue
      _valid_snap_name "$sname" || continue
      log "snap remove $sname"
      /usr/bin/snap remove "$sname" 2>/dev/null || true
    done < "$URSTACK_RESTORE_UNDO/snaps-installed.txt"
  fi
  if [[ -f "$URSTACK_RESTORE_UNDO/groups-added.txt" ]]; then
    uname=$(getent passwd "$CALLER_UID" | cut -d: -f1)
    while IFS= read -r g || [[ -n "$g" ]]; do
      [[ -n "$g" && "$g" =~ ^[A-Za-z0-9_-]+$ ]] || continue
      log "gpasswd -d $uname $g"
      gpasswd -d "$uname" "$g" 2>/dev/null || true
    done < "$URSTACK_RESTORE_UNDO/groups-added.txt"
  fi
  loc=$(tr -d '[:space:]' < "$URSTACK_RESTORE_UNDO/locale-before" 2>/dev/null || true)
  loc="${loc#LANG=}"
  if [[ -n "$loc" && "$loc" =~ ^[A-Za-z0-9_@.-]+$ ]]; then
    log "localectl set-locale LANG=$loc"
    localectl set-locale "LANG=$loc" || true
  fi
  km=$(tr -d '[:space:]' < "$URSTACK_RESTORE_UNDO/keymap-before" 2>/dev/null || true)
  if [[ -n "$km" && "$km" != "n/a" && "$km" =~ ^[A-Za-z0-9-]+$ ]]; then
    log "localectl set-keymap $km"
    localectl set-keymap "$km" || true
  fi
  if [[ -f "$URSTACK_RESTORE_UNDO/did-grub" && -f /etc/default/grub.urstack-bak ]]; then
    log "Restoring previous GRUB"
    install -o root -g root -m 0644 /etc/default/grub.urstack-bak /etc/default/grub || true
    _regenerate_grub || true
  fi
  if [[ -d "$URSTACK_RESTORE_UNDO/replaced" ]]; then
    while IFS= read -r -d '' f; do
      rel="${f#"$URSTACK_RESTORE_UNDO/replaced/"}"
      dkey="${rel%%/*}"
      rest="${rel#*/}"
      [[ "$rest" == "$rel" || "$rest" == *".."* || -z "$rest" ]] && continue
      dst=$(_restore_dest_for "$dkey") || continue
      install -D -o root -g root -m 0644 "$f" "$dst/$rest" || true
      log "  restored $dkey/$rest"
    done < <(find "$URSTACK_RESTORE_UNDO/replaced" -type f -print0 2>/dev/null)
  fi
  if [[ -f "$URSTACK_RESTORE_UNDO/added-files.txt" ]]; then
    while IFS= read -r rel || [[ -n "$rel" ]]; do
      dkey="${rel%%/*}"
      rest="${rel#*/}"
      [[ "$rest" == "$rel" || "$rest" == *".."* || -z "$rest" ]] && continue
      dst=$(_restore_dest_for "$dkey") || continue
      rm -f "$dst/$rest"
      log "  removed added $dkey/$rest"
    done < "$URSTACK_RESTORE_UNDO/added-files.txt"
  fi
  sysctl --system >/dev/null 2>&1 || true
  : > "$URSTACK_RESTORE_UNDO/priv-unwound"
  log "#unwound ok"
}

# Copy regular files from a caller-owned tree into a system directory, forcing
# root ownership. `cp -a` would preserve the modes and owner recorded in the
# backup, which for a blueprint written to exFAT or built on another machine can
# leave user-writable files in /etc — a way back in to root.
_install_tree_as_root() {
  local src="$1" dst="$2" dkey="${3:-}" rc=0 f rel
  mkdir -p "$dst" || return 1
  while IFS= read -r -d '' f; do
    rel="${f#"$src"/}"
    if [[ "$rel" == *".."* || "$rel" == /* ]]; then
      log "  refusing suspicious entry: $rel"; rc=1; continue
    fi
    # install(1) writes through an existing symlink, so drop one first.
    [[ -L "$dst/$rel" ]] && rm -f "$dst/$rel"
    [[ -n "$dkey" ]] && _undo_note_overwrite "$dst/$rel" "$dkey" "$rel"
    if install -D -o root -g root -m 0644 "$f" "$dst/$rel"; then
      log "  installed $rel"
    else
      log "  FAILED $rel"; rc=1
    fi
  done < <(find "$src" -type f -print0 2>/dev/null)
  return $rc
}

# A .repo file lets dnf fetch packages whose %post scriptlets run as root, so an
# unsigned or unverified repo out of a backup is a root code-execution path.
_repo_file_is_safe() {
  local f="$1"
  grep -qiE '^[[:space:]]*gpgcheck[[:space:]]*=[[:space:]]*0' "$f" && return 1
  grep -qiE '^[[:space:]]*gpgcheck[[:space:]]*=[[:space:]]*1' "$f" || return 1
  grep -qiE '^[[:space:]]*gpgkey[[:space:]]*=[[:space:]]*\S' "$f" || return 1
  return 0
}

# RPM Fusion .repo files from a backup set gpgkey=file:///etc/pki/rpm-gpg/...
# which only the official *-release RPMs install. Copying the .repo without
# those keys makes later `dnf install` from rpmfusion fail at GPG verify.
_is_rpmfusion_repo_file() {
  local base="$1"
  case "$base" in
    rpmfusion-*.repo) return 0 ;;
  esac
  return 1
}

_rpmfusion_release_rpm_urls() {
  local ver="$1"
  [[ "$ver" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' \
    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-${ver}.noarch.rpm" \
    "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-${ver}.noarch.rpm"
}

_rpmfusion_install_release() {
  local ver url f stash rc=0
  local -a urls=()
  ver=$(rpm -E %fedora 2>/dev/null || true)
  if [[ -z "$ver" || "$ver" == %fedora || ! "$ver" =~ ^[0-9]+$ ]]; then
    log "Could not detect Fedora version — cannot install RPM Fusion keys"
    return 1
  fi
  if rpm -q rpmfusion-free-release &>/dev/null \
     && rpm -q rpmfusion-nonfree-release &>/dev/null \
     && [[ -e /etc/pki/rpm-gpg/RPM-GPG-KEY-rpmfusion-free-fedora-$ver ]]; then
    log "RPM Fusion release packages already present (keys OK)"
    return 0
  fi
  mapfile -t urls < <(_rpmfusion_release_rpm_urls "$ver")
  if [[ ${#urls[@]} -ne 2 ]]; then
    log "Could not build RPM Fusion release RPM URLs"
    return 1
  fi
  for url in "${urls[@]}"; do
    [[ "$url" == https://* ]] || { log "refusing non-https RPM Fusion URL"; return 1; }
  done

  # A previous restore may have copied rpmfusion-*.repo that point at missing
  # key files. dnf then refuses the URL RPMs because it verifies them with a
  # key it cannot open. Stash those repos for this install.
  stash=$(mktemp -d /tmp/urstack-rpmfusion-XXXXXX) || return 1
  shopt -s nullglob
  for f in /etc/yum.repos.d/rpmfusion-*.repo; do
    mv "$f" "$stash/"
  done
  shopt -u nullglob

  log "Installing RPM Fusion release packages for Fedora $ver (provides repo GPG keys)…"
  if ! dnf install -y "${urls[@]}"; then
    log "RPM Fusion release install failed"
    shopt -s nullglob
    for f in "$stash"/*.repo; do
      mv "$f" /etc/yum.repos.d/
    done
    shopt -u nullglob
    rc=1
  fi
  rm -rf "$stash"
  return $rc
}

_install_repo_files() {
  local src="$1" dst="$2" rc=0 f base dest_base want_rpmfusion=0
  mkdir -p "$dst" || return 1
  shopt -s nullglob
  for f in "$src"/*.repo; do
    base=$(basename "$f")
    # Backups encode `:` as `%3A` so Copr names survive FAT/exFAT.
    dest_base=${base//%3A/:}
    dest_base=${dest_base//%3a/:}
    if [[ -L "$f" ]]; then
      log "  skipping symlink $base"; rc=1; continue
    fi
    if [[ "$dest_base" != *.repo || "$dest_base" == */* || "$dest_base" == *".."* ]]; then
      log "  skipping $base"; continue
    fi
    if _is_rpmfusion_repo_file "$dest_base"; then
      # Official *-release RPMs write these files and the GPG keys they name.
      want_rpmfusion=1
      log "  deferred $dest_base — enable via official release package (supplies GPG keys)"
      continue
    fi
    if ! _repo_file_is_safe "$f"; then
      # Unsigned vendor repos (e.g. Antigravity) are skipped, not installed.
      # Do not fail the whole step: Chrome/Copr still applied.
      log "  skipped $base — unsigned (gpgcheck=0 or no gpgkey)"
      continue
    fi
    [[ -L "$dst/$dest_base" ]] && rm -f "$dst/$dest_base"
    _undo_note_overwrite "$dst/$dest_base" yum_repos "$dest_base"
    if install -o root -g root -m 0644 "$f" "$dst/$dest_base"; then
      log "  installed $dest_base"
      # Log the URLs so an operator can audit what was just trusted.
      grep -hiE '^[[:space:]]*(baseurl|metalink|mirrorlist)[[:space:]]*=' "$f" \
        | sed 's/^[[:space:]]*/    /'
    else
      log "  FAILED $dest_base"; rc=1
    fi
  done
  shopt -u nullglob
  if [[ $want_rpmfusion -eq 1 ]]; then
    _rpmfusion_install_release || rc=1
  fi
  dnf makecache -y >/dev/null 2>&1 || true
  return $rc
}

_regenerate_grub() {
  local cfg=/boot/grub2/grub.cfg
  [[ -f "$cfg" ]] || cfg=/boot/efi/EFI/fedora/grub.cfg
  if [[ ! -f "$cfg" ]]; then
    log "restore_grub: no existing grub.cfg found — refusing to guess"
    return 1
  fi
  log "Regenerating $cfg"
  grub2-mkconfig -o "$cfg"
}

_validate_jobs_file "$JOBS_FILE"

# Health package installs: skip dead mirrors; don't let fastestmirror pin a 404 host.
_dnf_install() {
  dnf install -y --setopt=skip_if_unavailable=true --setopt=fastestmirror=false "$@"
}

run_akmods() {
  local newest running pid
  log "Building out-of-tree modules (akmods / NVIDIA) — may take a few minutes..."
  log "(Cancel the progress dialog to abort akmods.)"

  /usr/sbin/akmods &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [[ -n "$CANCEL_FLAG" && -e "$CANCEL_FLAG" ]]; then
      log "Cancel requested — stopping akmods (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 130
    fi
    sleep 1
  done
  wait "$pid" 2>/dev/null || true

  newest=$(rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort -V | tail -1)
  running=$(uname -r)
  if [[ -n "$newest" && "$newest" != "$running" ]]; then
    log "Ensuring kmods for new kernel: $newest"
    timeout "$TIMEOUT_AKMODS" /usr/sbin/akmods --kernels "$newest" --force || true
  fi
  log "akmods finished."
}

ensure_discover_exclude() {
  local conf=/etc/dnf/libdnf5.conf.d/99-exclude-discover.conf
  mkdir -p /etc/dnf/libdnf5.conf.d
  printf '%s\n' '[main]' 'excludepkgs=plasma-discover*' > "$conf"
  rm -f /etc/dnf/libdnf5.conf.d/99-exclude-discover-snap.conf
  log "Wrote $conf (does not remove an already-installed Discover)"
}

ensure_cursor_repo() {
  [[ -f /etc/yum.repos.d/cursor.repo ]] && return 0
  cat > /etc/yum.repos.d/cursor.repo <<'CURSORREPO'
[cursor]
name=Cursor
baseurl=https://downloads.cursor.com/yumrepo
enabled=1
gpgcheck=1
gpgkey=https://downloads.cursor.com/keys/anysphere.asc
repo_gpgcheck=1
CURSORREPO
  log "Created /etc/yum.repos.d/cursor.repo"
}

dnf_upgrade() {
  local -a excl=()
  local p
  if [[ "${EXCLUDE_DISCOVER:-1}" == "1" ]]; then
    for p in plasma-discover plasma-discover-notifier plasma-discover-packagekit \
             plasma-discover-flatpak plasma-discover-snap plasma-discover-kns \
             plasma-discover-offline-updates plasma-discover-libs plasma-discover-rpm-ostree; do
      excl+=(--exclude="$p")
    done
  fi
  log "DNF upgrade..."
  dnf upgrade -y "${excl[@]}"
  log "Syncing kernel-headers..."
  dnf upgrade -y kernel-headers || true
}

# Keep running kernel + newest KEEP_KERNELS-1 others (default keep 3 total)
prune_old_kernels() {
  local keep="${KEEP_KERNELS:-3}"
  local running
  running=$(uname -r)
  mapfile -t allk < <(rpm -q kernel --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' 2>/dev/null | sort -V)
  [[ ${#allk[@]} -le $keep ]] && { log "Kernel prune: ${#allk[@]} installed (≤ keep=$keep) — nothing to remove"; return 0; }

  local -A keep_set=()
  keep_set["$running"]=1
  local i count=1
  for (( i=${#allk[@]}-1; i>=0; i-- )); do
    [[ -n "${keep_set[${allk[i]}]:-}" ]] && continue
    keep_set["${allk[i]}"]=1
    count=$((count + 1))
    [[ $count -ge $keep ]] && break
  done

  local -a remove=()
  local k pkg
  for k in "${allk[@]}"; do
    [[ -n "${keep_set[$k]:-}" ]] && continue
    for pkg in "kernel-$k" "kernel-core-$k" "kernel-modules-$k" "kernel-modules-core-$k" \
               "kernel-modules-extra-$k" "kernel-devel-$k" "kernel-devel-matched-$k"; do
      rpm -q "$pkg" &>/dev/null && remove+=("$pkg")
    done
  done
  if [[ ${#remove[@]} -eq 0 ]]; then
    log "Kernel prune: nothing to remove"
    return 0
  fi
  log "Kernel prune: removing old kernels (keeping $keep including running $running)..."
  log "Removing: ${remove[*]}"
  dnf remove -y "${remove[@]}" || true

  local boot_mib
  boot_mib=$(df -Pm /boot 2>/dev/null | awk 'NR==2{print $4}')
  if [[ -n "$boot_mib" && "$boot_mib" -lt 300 ]]; then
    log "WARNING: /boot still low (${boot_mib} MiB). Consider removing rescue initramfs manually if needed."
  fi
}

exit_code=0
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == "#env "* ]]; then
    _ev="${line#\#env }"
    _ek="${_ev%%=*}"
    _eval="${_ev#*=}"
    # Each value is format-checked before export. KEEP_KERNELS in particular
    # lands in a `[[ -le ]]` arithmetic context, where a non-numeric value can
    # be made to evaluate a command substitution.
    _eok=0
    case "$_ek" in
      TIMEOUT_AKMODS|KEEP_KERNELS|EXCLUDE_DISCOVER)
        [[ "$_eval" =~ ^[0-9]+$ ]] && _eok=1 ;;
      HEALTH_JOURNAL_VACUUM)
        [[ "$_eval" =~ ^[0-9]+[KMGT]?$ ]] && _eok=1 ;;
      FEDORA_UPDATES_AKMODS_CANCEL|URSTACK_RESTORE_CANCEL|URSTACK_RESTORE_UNDO)
        [[ "$_eval" == /* && "$_eval" != *".."* ]] && _eok=1 ;;
    esac
    if [[ $_eok -eq 1 ]]; then
      printf -v "$_ek" '%s' "$_eval"
      # Exporting the variable *named* by $_ek is the intent; the name is
      # checked against the allowlist above before it gets here.
      # shellcheck disable=SC2163
      export "$_ek"
      [[ "$_ek" == FEDORA_UPDATES_AKMODS_CANCEL ]] && CANCEL_FLAG="$_eval"
    elif [[ -n "$_ek" ]]; then
      log "priv: ignoring malformed #env $_ek"
    fi
    continue
  fi
  [[ -z "$line" || "$line" =~ ^# ]] && continue
  # read -a splits on IFS without glob-expanding the fields, unlike `set -- $line`
  read -r -a _job_argv <<< "$line"
  [[ ${#_job_argv[@]} -gt 0 ]] || continue
  set -- "${_job_argv[@]}"
  job="$1"; shift || true
  if _restore_cancel_requested && [[ "$job" != restore_session_unwind ]]; then
    log "=== priv:$job skipped (restore cancelled) ==="
    log "#result $job skipped"
    continue
  fi
  log "=== priv:$job ==="
  _job_saved=$exit_code
  exit_code=0
  case "$job" in
    ensure_cursor_repo) ensure_cursor_repo || exit_code=1 ;;
    ensure_discover_exclude) ensure_discover_exclude || true ;;
    dnf_upgrade) dnf_upgrade || exit_code=1 ;;
    prune_old_kernels) prune_old_kernels || true ;;
    snap_refresh) snap refresh || exit_code=1 ;;
    ensure_snapd)
      _ensure_snapd_bin || exit_code=1
      ;;
    dnf_install_list)
      _dnf_list_label="${1:-}"
      if [[ ! "$_dnf_list_label" =~ ^[A-Za-z0-9_-]+$ ]]; then
        log "dnf_install_list: refusing label '${_dnf_list_label}'"; exit_code=1
      elif ! src=$(_b64_path "${2:-}"); then
        log "dnf_install_list: malformed path"; exit_code=1
      elif [[ -z "$src" || ! -f "$src" ]]; then
        log "dnf_install_list: missing file"; exit_code=1
      elif ! _valid_caller_owned_path "$src"; then
        log "dnf_install_list: refusing source not owned by the caller: $src"; exit_code=1
      else
        log "Installing packages from list…"
        names=()
        while IFS= read -r n || [[ -n "$n" ]]; do
          n="${n%%[[:space:]]*}"
          [[ -z "$n" || "$n" == \#* ]] && continue
          if ! _valid_pkg_name "$n"; then
            log "  ignoring invalid package name: $n"
            continue
          fi
          names+=("$n")
        done < "$src"
        if [[ ${#names[@]} -eq 0 ]]; then
          log "  empty list"
        else
          chunk=(); i=0
          _undo_note_dnf_before
          # Fedora ships ffmpeg-free; RPM Fusion's ffmpeg Conflicts with it.
          # skip-broken would skip ffmpeg rather than swapping.
          for n in "${names[@]}"; do
            if [[ "$n" == ffmpeg ]]; then
              if rpm -q ffmpeg-free &>/dev/null && ! rpm -q ffmpeg &>/dev/null; then
                log "Swapping ffmpeg-free → ffmpeg (RPM Fusion)…"
                dnf swap -y ffmpeg-free ffmpeg || log "ffmpeg swap skipped (will try skip-broken)"
              fi
              break
            fi
          done
          for n in "${names[@]}"; do
            if _restore_cancel_requested; then
              log "cancelled — not installing remaining packages"
              break
            fi
            chunk+=("$n"); i=$((i+1))
            if [[ $i -eq 80 ]]; then
              dnf install -y --skip-broken --skip-unavailable "${chunk[@]}" || exit_code=1
              chunk=(); i=0
            fi
          done
          if [[ ${#chunk[@]} -gt 0 ]] && ! _restore_cancel_requested; then
            dnf install -y --skip-broken --skip-unavailable "${chunk[@]}" || exit_code=1
          fi
        fi
      fi
      ;;
    dnf_install_pkg)
      n="${1:-}"
      if [[ -n "${2:-}" ]]; then
        log "dnf_install_pkg: extra arguments refused"; exit_code=1
      elif ! _valid_pkg_name "$n"; then
        log "dnf_install_pkg: refusing name '$n'"; exit_code=1
      else
        log "dnf install $n"
        dnf install -y --skip-broken --skip-unavailable "$n" || exit_code=1
      fi
      ;;
    usermod_add_groups)
      uname="${1:-}"
      glist="${2:-}"
      caller_name=$(getent passwd "$CALLER_UID" | cut -d: -f1)
      if [[ -n "${3:-}" ]]; then
        log "usermod_add_groups: extra arguments refused"; exit_code=1
      elif [[ -z "$uname" || "$uname" != "$caller_name" ]]; then
        log "usermod_add_groups: refusing user '$uname'"; exit_code=1
      else
        IFS=',' read -ra gs <<< "$glist"
        for g in "${gs[@]}"; do
          [[ -z "$g" ]] && continue
          if [[ ! "$g" =~ ^[A-Za-z0-9_-]+$ ]]; then
            log "  refusing group '$g'"; continue
          fi
          case "$g" in
            wheel|sudo|docker|lxd|libvirt|kvm)
              log "  skipped privileged group $g"; continue ;;
          esac
          if ! getent group "$g" >/dev/null 2>&1; then
            log "  missing group $g"; continue
          fi
          log "usermod -aG $g $uname"
          if usermod -aG "$g" "$uname"; then
            _valid_undo_dir && printf '%s\n' "$g" >> "$URSTACK_RESTORE_UNDO/groups-added.txt"
          else
            exit_code=1
          fi
        done
      fi
      ;;
    set_locale)
      loc="${1:-}"
      loc="${loc#LANG=}"
      if [[ -n "${2:-}" ]]; then
        log "set_locale: extra arguments refused"; exit_code=1
      elif [[ ! "$loc" =~ ^[A-Za-z0-9_@.-]+$ ]]; then
        log "set_locale: refusing '$loc'"; exit_code=1
      else
        _undo_note_locale_before
        log "localectl set-locale LANG=$loc"
        localectl set-locale "LANG=$loc" || exit_code=1
      fi
      ;;
    set_keymap)
      km="${1:-}"
      if [[ -n "${2:-}" ]]; then
        log "set_keymap: extra arguments refused"; exit_code=1
      elif [[ ! "$km" =~ ^[A-Za-z0-9-]+$ ]]; then
        log "set_keymap: refusing '$km'"; exit_code=1
      else
        _undo_note_locale_before
        log "localectl set-keymap $km"
        localectl set-keymap "$km" || exit_code=1
      fi
      ;;
    snap_install)
      sname="${1:-}"
      if [[ -n "${2:-}" ]]; then
        log "snap_install: extra arguments refused"; exit_code=1
      elif ! _valid_snap_name "$sname"; then
        log "snap_install: refusing name '$sname'"; exit_code=1
      elif ! _ensure_snapd_bin; then
        exit_code=1
      elif /usr/bin/snap list 2>/dev/null | awk -v n="$sname" 'NR>1 && $1==n {found=1} END{exit !found}'; then
        log "snap already installed: $sname"
      else
        log "Installing snap $sname…"
        if /usr/bin/snap install "$sname"; then
          _valid_undo_dir && printf '%s\n' "$sname" >> "$URSTACK_RESTORE_UNDO/snaps-installed.txt"
        else
          exit_code=1
        fi
      fi
      ;;
    fwupd_update) fwupdmgr update -y || fwupdmgr update || exit_code=1 ;;
    pip_build_deps)
      dnf install -y portaudio-devel cairo-devel cairo-gobject-devel \
        python3-devel libxkbcommon-devel openssl-devel libffi-devel || exit_code=1
      ;;
    cursor_rpm|install_local_rpm)
      rpm_path="${1:-}"
      if [[ -z "$rpm_path" || ! -f "$rpm_path" ]]; then
        log "$job: missing file"; exit_code=1
      elif ! _valid_caller_path "$rpm_path"; then
        log "$job: refusing untrusted path $rpm_path"; exit_code=1
      elif [[ "$rpm_path" != *.rpm ]]; then
        log "$job: not an .rpm path"; exit_code=1
      else
        # An RPM %post scriptlet runs as root, so the signature must be checked
        # even though this is a local file.
        dnf install -y --setopt=localpkg_gpgcheck=1 "$rpm_path" || exit_code=1
      fi
      ;;
    cursor_dnf) dnf upgrade cursor -y || exit_code=1 ;;
    akmods_wait)
      if command -v akmods &>/dev/null; then
        run_akmods || { ec=$?; [[ $ec -eq 130 ]] || exit_code=1; }
      fi
      ;;
    dnf_clean_all)
      log "Cleaning DNF / libdnf5 system caches…"
      if command -v dnf5 &>/dev/null; then
        dnf5 clean all || exit_code=1
      else
        dnf clean all || exit_code=1
      fi
      # Extra sweep — clean all does not always empty these trees fully
      rm -rf /var/cache/dnf/* /var/cache/libdnf5/* 2>/dev/null || true
      ;;
    dnf_autoremove)
      log "dnf autoremove…"
      if command -v dnf5 &>/dev/null; then
        dnf5 autoremove -y || exit_code=1
      else
        dnf autoremove -y || exit_code=1
      fi
      ;;
    coredump_vacuum)
      log "Removing systemd core dumps…"
      rm -f /var/lib/systemd/coredump/* 2>/dev/null || true
      ;;
    snap_purge_old)
      log "Removing disabled Snap revisions…"
      if command -v snap &>/dev/null; then
        snap list --all 2>/dev/null | awk 'NR>1 && /disabled/{print $1, $3}' \
          | while read -r sname srev; do
              [[ -n "$sname" && -n "$srev" ]] || continue
              log "snap remove $sname --revision=$srev"
              snap remove "$sname" --revision="$srev" || exit_code=1
            done
      fi
      ;;
    journal_vacuum)
      vac="${HEALTH_JOURNAL_VACUUM:-500M}"
      log "journalctl --vacuum-size=$vac"
      journalctl --vacuum-size="$vac" || exit_code=1
      ;;
    rpmfusion_enable)
      _rpmfusion_install_release || exit_code=1
      ;;
    codecs_install)
      log "Installing multimedia codecs…"
      # Fedora Workstation ships ffmpeg-free. RPM Fusion's ffmpeg Conflicts
      # with it, so a plain `dnf install ffmpeg` fails the whole job.
      _rpmfusion_install_release || log "RPM Fusion not fully enabled — some codec packages may be unavailable"
      if rpm -q ffmpeg &>/dev/null; then
        log "ffmpeg already installed"
      elif rpm -q ffmpeg-free &>/dev/null; then
        log "Swapping Fedora ffmpeg-free → RPM Fusion ffmpeg…"
        if ! dnf swap -y ffmpeg-free ffmpeg; then
          log "swap failed — retrying with --allowerasing"
          dnf install -y --allowerasing ffmpeg || exit_code=1
        fi
      else
        dnf install -y ffmpeg || {
          log "retrying ffmpeg with --allowerasing"
          dnf install -y --allowerasing ffmpeg || exit_code=1
        }
      fi
      dnf install -y --skip-unavailable --skip-broken \
        gstreamer1-plugin-libav \
        gstreamer1-plugins-ugly \
        gstreamer1-plugins-bad-free \
        gstreamer1-plugins-bad-freeworld \
        || log "some gstreamer plugins were skipped"
      if rpm -q ffmpeg &>/dev/null; then
        log "Multimedia codecs: ffmpeg $(rpm -q --qf '%{VERSION}' ffmpeg) installed"
      else
        log "ERROR: RPM Fusion ffmpeg is still not installed"
        exit_code=1
      fi
      ;;
    zram_enable)
      log "Installing zram-generator defaults…"
      dnf install -y zram-generator zram-generator-defaults || exit_code=1
      systemctl daemon-reload || true
      # Kick swap setup if unit exists
      systemctl start systemd-zram-setup@zram0.service 2>/dev/null || true
      ;;
    earlyoom_enable)
      log "Installing/enabling earlyoom…"
      dnf install -y earlyoom || exit_code=1
      systemctl enable --now earlyoom || exit_code=1
      ;;
    ppd_enable)
      log "Enabling power profiles…"
      # Fedora KDE ships tuned-ppd (same D-Bus API). It Conflicts: ppd-service
      # with power-profiles-daemon — never install both.
      if rpm -q tuned-ppd &>/dev/null; then
        log "tuned-ppd already installed — enabling it (not power-profiles-daemon)"
        systemctl enable --now tuned.service 2>/dev/null || true
        systemctl enable --now tuned-ppd.service || exit_code=1
      elif rpm -q power-profiles-daemon &>/dev/null; then
        log "power-profiles-daemon already installed — enabling it"
        systemctl unmask power-profiles-daemon.service 2>/dev/null || true
        systemctl enable --now power-profiles-daemon.service || exit_code=1
      else
        pkg=power-profiles-daemon
        if rpm -q plasma-workspace &>/dev/null || rpm -q plasma-desktop &>/dev/null; then
          pkg=tuned-ppd
        fi
        log "Installing $pkg…"
        _dnf_install "$pkg" || log "dnf reported errors — checking whether $pkg installed anyway…"
        if ! rpm -q "$pkg" &>/dev/null; then
          log "Retrying after a metadata refresh…"
          dnf clean metadata || true
          _dnf_install "$pkg" || true
        fi
        if rpm -q tuned-ppd &>/dev/null; then
          systemctl enable --now tuned.service 2>/dev/null || true
          systemctl enable --now tuned-ppd.service || exit_code=1
        elif rpm -q power-profiles-daemon &>/dev/null; then
          systemctl unmask power-profiles-daemon.service 2>/dev/null || true
          systemctl enable --now power-profiles-daemon.service || exit_code=1
        else
          log "ERROR: could not install $pkg (check DNF mirrors)"
          exit_code=1
        fi
      fi
      ;;
    tlp_disable)
      log "Disabling TLP…"
      systemctl disable --now tlp || exit_code=1
      ;;
    fstrim_all)
      log "fstrim -av…"
      fstrim -av || exit_code=1
      ;;
    dnf_speed_conf)
      log "Writing DNF speed drop-in…"
      mkdir -p /etc/dnf/dnf.conf.d /etc/dnf/libdnf5.conf.d
      cat > /etc/dnf/dnf.conf.d/99-urstack-speed.conf <<'EOF'
[main]
max_parallel_downloads=10
fastestmirror=True
EOF
      cat > /etc/dnf/libdnf5.conf.d/99-urstack-speed.conf <<'EOF'
[main]
max_parallel_downloads=10
fastestmirror=true
EOF
      log "Wrote 99-urstack-speed.conf"
      ;;
    sysctl_urstack)
      log "Writing /etc/sysctl.d/99-urstack.conf…"
      cat > /etc/sysctl.d/99-urstack.conf <<'EOF'
# Managed by UrStack Health
vm.swappiness=10
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=1024
EOF
      sysctl --system || exit_code=1
      ;;
    restore_etc_tree)
      dkey="${1:-}"
      if ! src=$(_b64_path "${2:-}"); then
        log "restore_etc_tree: malformed source argument"; exit_code=1
      elif ! dst=$(_restore_dest_for "$dkey"); then
        log "restore_etc_tree: unknown destination key '${dkey}'"; exit_code=1
      elif [[ -z "$src" || ! -d "$src" ]]; then
        log "restore_etc_tree: missing source directory '${src}'"; exit_code=1
      elif ! _valid_caller_owned_path "$src"; then
        log "restore_etc_tree: refusing source not owned by the caller: $src"; exit_code=1
      elif [[ "$dkey" == "yum_repos" ]]; then
        log "Installing DNF repositories into $dst…"
        _install_repo_files "$src" "$dst" || exit_code=1
      else
        log "Restoring $dkey into $dst…"
        _install_tree_as_root "$src" "$dst" "$dkey" || exit_code=1
        case "$dkey" in
          sysctl) sysctl --system >/dev/null || true ;;
          cups)   systemctl restart cups 2>/dev/null || true ;;
        esac
      fi
      ;;
    restore_grub)
      if ! src=$(_b64_path "${1:-}"); then
        log "restore_grub: malformed source argument"; exit_code=1
      elif [[ -z "$src" || ! -f "$src" ]]; then
        log "restore_grub: missing source file"; exit_code=1
      elif ! _valid_caller_owned_path "$src"; then
        log "restore_grub: refusing source not owned by the caller: $src"; exit_code=1
      elif ! grep -q '^GRUB_' "$src"; then
        log "restore_grub: source does not look like /etc/default/grub"; exit_code=1
      else
        cp -a /etc/default/grub /etc/default/grub.urstack-bak 2>/dev/null || true
        log "Previous /etc/default/grub saved as /etc/default/grub.urstack-bak"
        if install -o root -g root -m 0644 "$src" /etc/default/grub; then
          _valid_undo_dir && : > "$URSTACK_RESTORE_UNDO/did-grub"
          _regenerate_grub || exit_code=1
        else
          log "restore_grub: could not write /etc/default/grub"; exit_code=1
        fi
      fi
      ;;
    health_restore_files)
      rp_dir="${1:-}"
      files="$rp_dir/files"
      rp_root="$CALLER_HOME/.local/state/stackup/health-restore-points"
      if [[ -z "$rp_dir" || ! -d "$files" ]]; then
        log "health_restore_files: missing $rp_dir/files"; exit_code=1
      elif [[ "$rp_dir" != "$rp_root"/* || "$rp_dir" == *".."* ]]; then
        log "health_restore_files: refusing path outside $rp_root"; exit_code=1
      elif ! _valid_caller_path "$rp_dir"; then
        log "health_restore_files: refusing untrusted path $rp_dir"; exit_code=1
      else
        log "Restoring UrStack-managed config files from $rp_dir…"
        # sysctl drop-in
        if [[ -f "$files/99-urstack.conf.state" ]]; then
          st=$(tr -d '[:space:]' < "$files/99-urstack.conf.state")
          if [[ "$st" == present && -f "$files/99-urstack.conf" ]]; then
            cp -a "$files/99-urstack.conf" /etc/sysctl.d/99-urstack.conf
          else
            rm -f /etc/sysctl.d/99-urstack.conf
          fi
          sysctl --system || true
        fi
        # DNF speed drop-ins
        if [[ -f "$files/99-urstack-speed-dnf.conf.state" ]]; then
          st=$(tr -d '[:space:]' < "$files/99-urstack-speed-dnf.conf.state")
          if [[ "$st" == present && -f "$files/99-urstack-speed-dnf.conf" ]]; then
            mkdir -p /etc/dnf/dnf.conf.d
            cp -a "$files/99-urstack-speed-dnf.conf" /etc/dnf/dnf.conf.d/99-urstack-speed.conf
          else
            rm -f /etc/dnf/dnf.conf.d/99-urstack-speed.conf
          fi
        fi
        if [[ -f "$files/99-urstack-speed-libdnf5.conf.state" ]]; then
          st=$(tr -d '[:space:]' < "$files/99-urstack-speed-libdnf5.conf.state")
          if [[ "$st" == present && -f "$files/99-urstack-speed-libdnf5.conf" ]]; then
            mkdir -p /etc/dnf/libdnf5.conf.d
            cp -a "$files/99-urstack-speed-libdnf5.conf" /etc/dnf/libdnf5.conf.d/99-urstack-speed.conf
          else
            rm -f /etc/dnf/libdnf5.conf.d/99-urstack-speed.conf
          fi
        fi
        log "Config files restored"
      fi
      ;;
    dnf_history_rollback)
      hid="${1:-}"
      if [[ -z "$hid" || "$hid" == "0" ]]; then
        log "dnf_history_rollback: no history id — skip"
      elif [[ ! "$hid" =~ ^[0-9]+$ ]]; then
        log "dnf_history_rollback: invalid transaction id"; exit_code=1
      else
        log "dnf history rollback $hid…"
        # Non-interactive rollback to the transaction that existed before Health apply
        dnf history rollback -y "$hid" || exit_code=1
      fi
      ;;
    unit_set_enabled)
      unit="${1:-}"
      state="${2:-}"
      if [[ -z "$unit" ]]; then
        log "unit_set_enabled: missing unit"; exit_code=1
      elif ! _valid_unit_name "$unit"; then
        log "unit_set_enabled: refusing unit name '$unit'"; exit_code=1
      else
        case "$state" in
          enabled)
            log "systemctl enable --now $unit"
            systemctl enable --now "$unit" || exit_code=1
            ;;
          disabled|masked)
            log "systemctl disable --now $unit"
            systemctl disable --now "$unit" || true
            ;;
          *)
            log "unit_set_enabled: leave $unit as-is (was $state)"
            ;;
        esac
      fi
      ;;
    restore_session_unwind)
      _priv_unwind_restore_session || exit_code=1
      ;;
    *) log "Unknown job: $job"; exit_code=1 ;;
  esac
  _tag="$job"
  case "$job" in
    restore_etc_tree|snap_install|dnf_install_list) _tag="$job:${1:-}" ;;
  esac
  if [[ $exit_code -eq 0 ]]; then
    log "#result $_tag ok"
  else
    log "#result $_tag fail"
  fi
  if [[ $_job_saved -ne 0 || $exit_code -ne 0 ]]; then
    exit_code=1
  else
    exit_code=0
  fi
done < "$JOBS_FILE"

if _restore_cancel_requested && [[ ! -f "${URSTACK_RESTORE_UNDO:-}/priv-unwound" ]]; then
  _priv_unwind_restore_session || true
fi

exit "$exit_code"
