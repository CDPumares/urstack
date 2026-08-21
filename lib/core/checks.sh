# Check functions for Fedora Updates (sourced).
# shellcheck shell=bash

# OCI fedora remote lists phantom digest bumps; exclude known noise from "has updates"
is_flatpak_oci_noise() {
  local app="$1"
  case "$app" in
    org.fedoraproject.Platform|org.fedoraproject.Platform.*) return 0 ;;
    org.fedoraproject.Gtk3theme.*) return 0 ;;
    *.Locale|*.Debug|*.Sources) return 0 ;;
  esac
  return 1
}

# True when remote-ls claims an update but the remote commit is already installed
# (common on Fedora's OCI remotes — same payload, different commit id / Alt-id).
flatpak_update_is_phantom() {
  local app="$1" origin="${2:-}"
  local info remote_commit
  info=$(flatpak info "$app" 2>/dev/null) || return 1
  [[ -n "$origin" ]] || return 1

  remote_commit=$(flatpak remote-info --system "$origin" "$app" 2>/dev/null \
    | awk '/^[[:space:]]*Commit:/{print $NF; exit}') || true
  if [[ -z "$remote_commit" ]]; then
    remote_commit=$(flatpak remote-info --user "$origin" "$app" 2>/dev/null \
      | awk '/^[[:space:]]*Commit:/{print $NF; exit}') || true
  fi
  [[ -n "$remote_commit" ]] || return 1

  # Installed Commit or any Alt-id matches the remote → nothing to install
  if printf '%s\n' "$info" | awk '/^[[:space:]]*(Commit|Alt-id):/{print $NF}' \
      | grep -qx "$remote_commit"; then
    return 0
  fi
  return 1
}

check_dnf() {
  command -v dnf &>/dev/null || return 0
  local out ec=0
  local -a excl=()
  mapfile -t excl < <(dnf_exclude_args)
  out=$(timeout "$TIMEOUT_DNF" dnf check-update -q "${excl[@]}" 2>/dev/null) || ec=$?
  # dnf: 0 = none, 100 = updates available, 124 = timeout
  if [[ $ec -eq 124 ]]; then
    echo "DNF check timed out after ${TIMEOUT_DNF}s — metadata refresh may still be running" \
      >> "$check_dir/preflight_advisories"
    return 0
  fi
  out=$(printf '%s\n' "$out" | grep -Ev '^\s*$|^(Upgrades|Obsoletes|Available|Removed|Downgraded)$') || true
  local p
  for p in "${DNF_EXCLUDE_PKGS[@]}"; do
    out=$(printf '%s\n' "$out" | grep -Ev "^${p}\.") || true
  done
  local count=0
  [[ -n "$out" ]] && count=$(printf '%s\n' "$out" | grep -vc '^\s*$') || true
  if [[ $count -gt 0 ]]; then
    echo "$out"   > "$check_dir/dnf"
    echo "$count" > "$check_dir/dnf_count"
    local pkgs snip
    pkgs=$(printf '%s\n' "$out" | awk '{print $1}' | sed 's/\.[^.]*$//' | head -n 6 | tr '\n' ' ')
    if [[ -n "$pkgs" ]]; then
      snip=$(timeout 25 dnf -C changelog --upgrades --count=2 $pkgs 2>/dev/null | head -n 80) || true
      [[ -n "$snip" ]] && printf '%s\n' "$snip" > "$check_dir/dnf_changelog"
    fi
  fi
}

check_snap() {
  command -v snap &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_SNAP" snap refresh --list 2>&1) || true
  if [[ -n "$out" ]] && ! echo "$out" | grep -qi "all snaps up to date"; then
    echo "$out" > "$check_dir/snap"
  fi
}

# True when fwupdmgr get-updates output lists a real payload (not inventory noise).
fwupd_output_has_updates() {
  local out="$1"
  [[ -n "$out" ]] || return 1
  echo "$out" | grep -qiE 'Update Version:|Upgrade Version:' && return 0
  return 1
}

check_fw() {
  command -v fwupdmgr &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_FW" fwupdmgr get-updates 2>&1) || true
  if fwupd_output_has_updates "$out"; then
    echo "$out" > "$check_dir/fw"
  fi
}

check_flatpak() {
  command -v flatpak &>/dev/null || return 0
  # Metadata refresh only (does not install apps)
  timeout "$TIMEOUT_FLATPAK" flatpak update --appstream -y --system &>/dev/null || true
  timeout "$TIMEOUT_FLATPAK" flatpak update --appstream -y --user &>/dev/null || true

  local line app ver branch origin real_out="" noise_out="" count=0
  local sys_ls user_ls

  sys_ls=$(timeout "$TIMEOUT_FLATPAK" flatpak remote-ls --updates --system \
    --columns=application,version,branch,origin 2>/dev/null) || true
  user_ls=$(timeout "$TIMEOUT_FLATPAK" flatpak remote-ls --updates --user \
    --columns=application,version,branch,origin 2>/dev/null) || true

  while IFS=$'\t' read -r app ver branch origin || [[ -n "${app:-}" ]]; do
    [[ -z "${app:-}" ]] && continue
    # Fallback if remote-ls used spaces instead of tabs
    if [[ -z "${origin:-}" && "$app" == *" "* ]]; then
      origin=$(awk '{print $NF}' <<< "$app")
      ver=$(awk '{print $2}' <<< "$app")
      branch=$(awk '{print $3}' <<< "$app")
      app=$(awk '{print $1}' <<< "$app")
    fi
    if is_flatpak_oci_noise "$app"; then
      noise_out+="(oci noise) $app  $ver  [$origin]"$'\n'
      continue
    fi
    # Fedora OCI often lists apps (e.g. Extensions) that flatpak update will not change
    if flatpak_update_is_phantom "$app" "$origin"; then
      noise_out+="(already current) $app  $ver  [$origin]"$'\n'
      continue
    fi
    real_out+="$app  ${ver:-?}  ${branch:-}  [$origin]"$'\n'
    count=$((count + 1))
  done <<< "$(printf '%s\n%s\n' "$sys_ls" "$user_ls" | tr -s ' ' '\t' | sed 's/\t\t/\t/g')"

  if [[ $count -gt 0 ]]; then
    printf '%s' "$real_out" > "$check_dir/flatpak"
    echo "$count" > "$check_dir/flatpak_count"
  fi
  if [[ -n "$noise_out" ]]; then
    printf 'Fedora OCI remotes often list digest-only rebuilds that install nothing.\n%s' \
      "$noise_out" > "$check_dir/flatpak_eol"
  fi
}

check_toolbox() {
  command -v toolbox &>/dev/null || return 0
  command -v podman &>/dev/null || return 0
  local names name check count out="" status
  names=$(podman ps -a --filter label=com.github.containers.toolbox=true \
    --format '{{.Names}}' 2>/dev/null) || true
  [[ -z "$names" ]] && return 0

  : > "$check_dir/toolbox_names"
  while IFS= read -r name; do
    [[ -z "$name" ]] && continue
    status=$(podman inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo unknown)
    check=$(timeout "$TIMEOUT_TOOLBOX" toolbox run -c "$name" -- \
      dnf check-update -q 2>/dev/null) || true
    check=$(printf '%s\n' "$check" | grep -Ev \
      '^\s*$|^(Upgrades|Obsoletes|Available|Removed|Downgraded)$') || true
    if [[ -n "$check" ]]; then
      count=$(printf '%s\n' "$check" | grep -vc '^\s*$') || count=0
      out+="=== toolbox:${name} [${status}] (${count} package(s)) ==="$'\n'"$check"$'\n'
      printf '%s\n' "$name" >> "$check_dir/toolbox_names"
    fi
  done <<< "$names"

  [[ -n "$out" ]] && printf '%s' "$out" > "$check_dir/toolbox"
}

check_distrobox() {
  command -v distrobox &>/dev/null || return 0
  local names name check count out="" status
  names=$(distrobox list --no-color 2>/dev/null | awk -F'|' 'NR>1 {
    n=$2; gsub(/^[ \t]+|[ \t]+$/, "", n); if (n != "" && n != "NAME") print n
  }') || true
  [[ -z "$names" ]] && return 0

  : > "$check_dir/distrobox_names"
  while IFS= read -r name; do
    [[ -z "$name" || "$name" == "ID" || "$name" == "NAME" ]] && continue
    status=$(podman inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo unknown)
    check=$(timeout "$TIMEOUT_TOOLBOX" distrobox enter -n "$name" -- \
      bash -lc '
        if command -v dnf >/dev/null; then
          dnf check-update -q || true
        elif command -v apt-get >/dev/null; then
          apt-get -s upgrade 2>/dev/null | grep -E "^Inst " || true
        fi
      ' 2>/dev/null) || true
    check=$(printf '%s\n' "$check" | grep -Ev \
      '^\s*$|^(Upgrades|Obsoletes|Available|Removed|Downgraded)$') || true
    if [[ -n "$check" ]]; then
      count=$(printf '%s\n' "$check" | grep -vc '^\s*$') || count=0
      out+="=== distrobox:${name} [${status}] (${count} package(s)) ==="$'\n'"$check"$'\n'
      printf '%s\n' "$name" >> "$check_dir/distrobox_names"
    fi
  done <<< "$names"

  if [[ -n "$out" ]]; then
    printf '%s' "$out" > "$check_dir/distrobox"
  fi
}

check_npm() {
  command -v npm &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_NPM" npm outdated -g 2>/dev/null | tail -n +2) || true
  [[ -n "$out" ]] && echo "$out" > "$check_dir/npm"
}

check_npm_user() {
  command -v npm &>/dev/null || return 0
  local prefix="$HOME/.local"
  [[ -d "$prefix/lib/node_modules" ]] || return 0
  local out
  out=$(timeout "$TIMEOUT_NPM" npm outdated -g --prefix "$prefix" 2>/dev/null | tail -n +2) || true
  [[ -n "$out" ]] && echo "$out" > "$check_dir/npm_user"
}

check_pip() {
  command -v python3 &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_PIP" python3 -m pip list --user --outdated 2>/dev/null | tail -n +3) || true
  [[ -n "$out" ]] && echo "$out" > "$check_dir/pip"
}

check_pipx() {
  command -v pipx &>/dev/null || return 0
  # Only flag when pipx reports something outdated
  local out
  if pipx upgrade-all --help 2>&1 | grep -q -- '--dry-run'; then
    out=$(timeout "$TIMEOUT_PIPX" pipx upgrade-all --dry-run 2>&1) || true
    if echo "$out" | grep -qiE 'would upgrade|upgraded|→|->'; then
      echo "$out" > "$check_dir/pipx"
    fi
  fi
  # No --dry-run: skip rather than flag every installed tool as pending.
}

check_rust() {
  command -v rustup &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_RUST" rustup check 2>&1) || true
  echo "$out" | grep -q "updates available" && echo "$out" > "$check_dir/rust" || true
}

check_cargo() {
  command -v cargo &>/dev/null || return 0
  local out
  out=$(timeout "$TIMEOUT_CARGO" cargo install-update -l 2>/dev/null) || true
  if [[ -n "$out" ]] && echo "$out" | grep -q "Yes"; then
    echo "$out" > "$check_dir/cargo"
  fi
}

check_node() {
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  [[ -f "$nvm_dir/nvm.sh" ]] || return 0
  # shellcheck source=/dev/null
  . "$nvm_dir/nvm.sh" 2>/dev/null || return 0
  local current latest stream
  current=$(nvm current 2>/dev/null) || true
  [[ -z "$current" || "$current" == "none" || "$current" == "system" ]] && return 0
  stream=$(cat "$nvm_dir/alias/default" 2>/dev/null || echo node)
  [[ -n "$stream" ]] || stream=node
  latest=$(timeout "$TIMEOUT_NODE" nvm version-remote "$stream" 2>/dev/null) || true
  if [[ -n "$latest" ]] && version_gt "$latest" "$current"; then
    printf 'current=%s\nlatest=%s\nstream=%s\n' "$current" "$latest" "$stream" > "$check_dir/node"
  fi
}

check_cursor() {
  command -v rpm &>/dev/null || return 0
  rpm -q cursor &>/dev/null || return 0
  local installed latest url hash rpm_url http_code body
  local plat="linux-x64" rpm_dir="linux/x64/rpm/x86_64" rpm_arch="x86_64"
  case "$(uname -m)" in
    aarch64|arm64) plat="linux-arm64"; rpm_dir="linux/arm64/rpm/aarch64"; rpm_arch="aarch64" ;;
  esac
  installed=$(rpm -q --qf '%{VERSION}' cursor 2>/dev/null) || return 0
  [[ -n "$installed" ]] || return 0

  body=$(mktemp)
  http_code=$(timeout "$TIMEOUT_CURSOR" curl -sS -o "$body" -w '%{http_code}' \
    "https://api2.cursor.sh/updates/api/update/${plat}/cursor/${installed}/stable" 2>/dev/null) || true

  if [[ "$http_code" == "200" && -s "$body" ]]; then
    latest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' "$body" 2>/dev/null) || true
    url=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("url",""))' "$body" 2>/dev/null) || true
    rm -f "$body"
    if version_gt "$latest" "$installed" && [[ -n "$url" ]]; then
      hash=$(printf '%s' "$url" | sed -n 's|.*/production/\([^/]*\)/.*|\1|p')
      if [[ -n "$hash" ]]; then
        rpm_url="https://downloads.cursor.com/production/${hash}/${rpm_dir}/cursor-${latest}.el8.${rpm_arch}.rpm"
        printf 'current=%s\nlatest=%s\nsource=update-api\nrpm_url=%s\n' \
          "$installed" "$latest" "$rpm_url" > "$check_dir/cursor_meta"
        printf 'Current: %s\nLatest:  %s\n(via Cursor update API — yum repo is behind)\n' \
          "$installed" "$latest" > "$check_dir/cursor"
        return 0
      fi
    fi
  else
    rm -f "$body"
  fi
}

derive_cursor_from_dnf() {
  [[ -f "$check_dir/cursor" ]] && return 0
  [[ -f "$check_dir/dnf" ]] || return 0
  local cursor_lines
  cursor_lines=$(grep -E '^cursor\.' "$check_dir/dnf" 2>/dev/null) || true
  if [[ -n "$cursor_lines" ]]; then
    echo "$cursor_lines" > "$check_dir/cursor"
    printf 'source=dnf\n' > "$check_dir/cursor_meta"
  fi
}

check_claude() {
  command -v claude &>/dev/null || return 0
  command -v npm &>/dev/null || return 0
  local current latest
  current=$(claude --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) || true
  latest=$(timeout "$TIMEOUT_CLAUDE" npm view @anthropic-ai/claude-code version 2>/dev/null) || true
  if version_gt "$latest" "$current"; then
    printf 'Current: %s\nLatest:  %s\n' "$current" "$latest" > "$check_dir/claude"
    printf 'current=%s\nlatest=%s\n' "$current" "$latest" > "$check_dir/claude_meta"
  fi
}

check_supabase() {
  command -v supabase &>/dev/null || return 0
  local ver_out current latest
  ver_out=$(timeout "$TIMEOUT_SUPABASE" supabase --version 2>&1) || true
  current=$(printf '%s' "$ver_out" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) || true
  latest=$(printf '%s' "$ver_out" | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | sed -n '2p' | tr -d 'v') || true
  if [[ -z "$latest" ]]; then
    latest=$(timeout "$TIMEOUT_SUPABASE" curl -sS \
      'https://api.github.com/repos/supabase/cli/releases/latest' 2>/dev/null \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["tag_name"].lstrip("v"))' 2>/dev/null) || true
  fi
  if version_gt "$latest" "$current"; then
    printf 'Current: %s\nLatest:  %s\n' "$current" "$latest" > "$check_dir/supabase"
    printf 'current=%s\nlatest=%s\n' "$current" "$latest" > "$check_dir/supabase_meta"
  fi
}

# Optional light health: JetBrains Toolbox / AppImages present but unmanaged
check_appimage_health() {
  local -a found=()
  local d f size mtime
  for d in "$HOME/Applications" "$HOME/AppImages" "$HOME/bin"; do
    [[ -d "$d" ]] || continue
    while IFS= read -r -d '' f; do
      found+=("$f")
    done < <(find "$d" -maxdepth 2 -type f -name '*.AppImage' -print0 2>/dev/null)
  done
  if [[ ${#found[@]} -gt 0 ]]; then
    {
      echo "Found ${#found[@]} AppImage(s) — update these manually (not managed by DNF/Flatpak/Snap):"
      for f in "${found[@]}"; do
        size=$(du -h "$f" 2>/dev/null | awk '{print $1}')
        mtime=$(date -r "$f" '+%Y-%m-%d' 2>/dev/null || true)
        printf '  %s  (%s, modified %s)\n' "$f" "${size:-?}" "${mtime:-?}"
      done
    } > "$check_dir/appimage_advisory"
  fi
}

check_jetbrains_toolbox() {
  local tb_bin="" channel_json="" ver="" apps_dir="" app_count=0
  for tb_bin in \
      "$HOME/.local/share/JetBrains/Toolbox/bin/jetbrains-toolbox" \
      "$HOME/.local/bin/jetbrains-toolbox" \
      /usr/bin/jetbrains-toolbox; do
    [[ -x "$tb_bin" ]] && break
    tb_bin=""
  done
  [[ -z "$tb_bin" ]] && [[ ! -d "$HOME/.local/share/JetBrains/Toolbox" ]] && return 0

  apps_dir="$HOME/.local/share/JetBrains/Toolbox/apps"
  [[ -d "$apps_dir" ]] && app_count=$(find "$apps_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)

  channel_json=$(find "$HOME/.local/share/JetBrains/Toolbox" -name '.channel.settings.json' 2>/dev/null | head -1) || true
  if [[ -n "$channel_json" && -f "$channel_json" ]]; then
    ver=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("version") or d.get("build") or "")' \
      "$channel_json" 2>/dev/null) || true
  fi
  if [[ -z "$ver" && -n "$tb_bin" ]]; then
    ver=$("$tb_bin" --version 2>/dev/null | head -1) || true
  fi

  {
    echo "JetBrains Toolbox is installed separately — updates via the Toolbox app (not DNF)."
    [[ -n "$tb_bin" ]] && echo "Binary: $tb_bin"
    [[ -n "$ver" ]] && echo "Version: $ver"
    echo "Managed IDE installs under Toolbox/apps: $app_count"
    echo "Open Toolbox periodically to apply IDE updates."
  } > "$check_dir/jetbrains_advisory"
}

# Keep Cursor on its own card — do not also list it under DNF.
strip_cursor_from_dnf() {
  [[ -f "$check_dir/cursor" && -f "$check_dir/dnf" ]] || return 0
  local filtered count=0
  filtered=$(grep -Ev '^cursor\.' "$check_dir/dnf" 2>/dev/null) || true
  if [[ -n "$filtered" ]]; then
    printf '%s\n' "$filtered" > "$check_dir/dnf"
    count=$(printf '%s\n' "$filtered" | grep -vc '^\s*$') || count=0
    echo "$count" > "$check_dir/dnf_count"
  else
    rm -f "$check_dir/dnf" "$check_dir/dnf_count"
  fi
}

run_all_checks_parallel() {
  local -a _pids=()
  _check_spawn() {
    while (( $(jobs -rp | wc -l) >= ${CHECK_PARALLEL:-4} )); do
      wait -n 2>/dev/null || true
    done
    "$@" &
    _pids+=($!)
  }
  source_enabled dnf      && _check_spawn check_dnf
  source_enabled snap     && _check_spawn check_snap
  source_enabled fw       && _check_spawn check_fw
  source_enabled flatpak  && _check_spawn check_flatpak
  source_enabled toolbox  && _check_spawn check_toolbox
  source_enabled toolbox  && _check_spawn check_distrobox
  source_enabled npm      && _check_spawn check_npm
  source_enabled npm_user && _check_spawn check_npm_user
  source_enabled pip      && _check_spawn check_pip
  source_enabled pipx     && _check_spawn check_pipx
  source_enabled rust     && _check_spawn check_rust
  source_enabled cargo    && _check_spawn check_cargo
  source_enabled node     && _check_spawn check_node
  source_enabled cursor   && _check_spawn check_cursor
  source_enabled claude   && _check_spawn check_claude
  source_enabled supabase && _check_spawn check_supabase
  [[ "$(cfg_get enable_appimage 0)" == "1" ]] && _check_spawn check_appimage_health
  [[ "$(cfg_get enable_jetbrains 0)" == "1" ]] && _check_spawn check_jetbrains_toolbox
  local _p
  for _p in "${_pids[@]}"; do wait "$_p" 2>/dev/null || true; done
  source_enabled cursor && derive_cursor_from_dnf
  source_enabled cursor && strip_cursor_from_dnf
  # Merge distrobox advisory into toolbox section for one UI card
  if [[ -f "$check_dir/distrobox" ]]; then
    if [[ -f "$check_dir/toolbox" ]]; then
      printf '\n%s' "$(cat "$check_dir/distrobox")" >> "$check_dir/toolbox"
    else
      cp "$check_dir/distrobox" "$check_dir/toolbox"
    fi
  fi
}
