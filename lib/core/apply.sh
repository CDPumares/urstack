# Apply / UI helpers for UrStack (sourced).
# shellcheck shell=bash

emit_progress() {
  echo "${current_pct:-0}"
  echo "# $1"
}

# True when firmware apply should be skipped for this selection.
# Opt-in: Settings apply_fw=1, or --include-firmware.
# Explicit Apply-checklist picks (selected != "all") still install firmware
# even when apply_fw is off — that is the one-off tick.
skip_firmware() {
  local selected="${1:-all}"
  [[ "${INCLUDE_FIRMWARE:-0}" == "1" ]] && return 1
  [[ "$(cfg_get apply_fw 0)" == "1" ]] && return 1
  [[ "$selected" != "all" ]] && return 1
  return 0
}

# Write TRUE/FALSE|id|label lines for the Apply checklist.
write_apply_sections_file() {
  local dest="${1:-}"
  [[ -n "$dest" ]] || return 1
  : > "$dest"
  local s pre
  for s in "${SECTION_KEYS[@]}"; do
    has_section "$s" || continue
    pre=TRUE
    if [[ "$s" == "fw" && "${INCLUDE_FIRMWARE:-0}" != "1" && "$(cfg_get apply_fw 0)" != "1" ]]; then
      pre=FALSE
    fi
    printf '%s|%s|%s\n' "$pre" "$s" "$(section_label "$s")" >> "$dest"
  done
}

# Consume zenity-compatible progress lines (# text / 0-100) via GTK UI (zenity fallback)
pipe_to_progress() {
  local title="$1"
  local pulsate="${2:-0}"
  local cancel_flag="${3:-}"
  # In-app shell owns the progress UI — just forward lines on stdout.
  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    cat
    return 0
  fi
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  if [[ "${AUTO_YES:-0}" -eq 0 && -n "${DISPLAY:-}" && -x "$_ui" ]] && command -v python3 &>/dev/null; then
    local -a ui_args=(python3 "$_ui" progress --title "$title" --auto-close)
    [[ "$pulsate" == "1" ]] && ui_args+=(--pulsate)
    [[ -n "$cancel_flag" ]] && ui_args+=(--cancel-flag "$cancel_flag")
    "${ui_args[@]}"
    return $?
  fi
  if command -v zenity &>/dev/null && [[ "${AUTO_YES:-0}" -eq 0 ]]; then
    if [[ "$pulsate" == "1" ]]; then
      zenity --progress --title="$title" --pulsate --width=700 --height=420 \
        --text="Working…" --auto-close || { [[ -n "$cancel_flag" ]] && touch "$cancel_flag"; return 1; }
    else
      zenity --progress --title="$title" --percentage=0 --width=700 --height=500 \
        --text="Working…" --auto-close --auto-kill || { [[ -n "$cancel_flag" ]] && touch "$cancel_flag"; return 1; }
    fi
    return $?
  fi
  cat
  return 0
}

# Build privileged jobs file from selected sections; returns path via echo
build_priv_jobs() {
  local selected="$1"
  local jobs
  jobs=$(mktemp)
  local s

  if has_section cursor && section_is_selected cursor "$selected"; then
    echo "ensure_cursor_repo" >> "$jobs"
  fi
  if has_section dnf && section_is_selected dnf "$selected" && [[ "${EXCLUDE_DISCOVER:-1}" == "1" ]]; then
    echo "ensure_discover_exclude" >> "$jobs"
  fi

  for s in "${SECTION_KEYS[@]}"; do
    has_section "$s" || continue
    section_is_selected "$s" "$selected" || continue
    case "$s" in
      dnf)
        echo "dnf_upgrade" >> "$jobs"
        if [[ "${ENABLE_KERNEL_PRUNE:-1}" == "1" ]]; then
          echo "prune_old_kernels" >> "$jobs"
        fi
        echo "akmods_wait" >> "$jobs"
        ;;
      snap) echo "snap_refresh" >> "$jobs" ;;
      fw)
        if skip_firmware "$selected"; then
          echo "# skipped fw (enable Apply firmware in Settings, or pass --include-firmware)" >> "$jobs"
        else
          echo "fwupd_update" >> "$jobs"
        fi
        ;;
      pip) echo "pip_build_deps" >> "$jobs" ;;
      cursor)
        local cursor_source rpm_url
        cursor_source=$(grep '^source=' "$check_dir/cursor_meta" 2>/dev/null | cut -d= -f2-) || true
        rpm_url=$(grep '^rpm_url=' "$check_dir/cursor_meta" 2>/dev/null | cut -d= -f2-) || true
        if [[ "$cursor_source" == "update-api" && -n "$rpm_url" ]]; then
          local rpm_tmp
          rpm_tmp=$(mktemp --suffix=.rpm)
          if curl -fsSL -o "$rpm_tmp" "$rpm_url"; then
            echo "cursor_rpm $rpm_tmp" >> "$jobs"
            echo "$rpm_tmp" >> "$check_dir/cursor_rpm_tmp"
          else
            echo "cursor_dnf" >> "$jobs"
            rm -f "$rpm_tmp"
          fi
        else
          echo "cursor_dnf" >> "$jobs"
        fi
        ;;
    esac
  done

  # If only ensure_* lines, still OK
  if [[ ! -s "$jobs" ]]; then
    rm -f "$jobs"
    echo ""
    return 0
  fi
  echo "$jobs"
}

run_priv_jobs() {
  local jobs_file="$1"
  [[ -n "$jobs_file" && -f "$jobs_file" ]] || return 0

  local cancel_flag
  cancel_flag=$(mktemp)
  rm -f "$cancel_flag"  # existence means cancel
  FEDORA_UPDATES_AKMODS_CANCEL="$cancel_flag" priv_jobs_inject_env "$jobs_file"

  local ec=0
  if [[ "${AUTO_YES:-0}" -eq 0 && -n "${DISPLAY:-}" ]]; then
    (
      pkexec_priv "$jobs_file" 2>&1 | section_log priv | emit_lines
      echo "${PIPESTATUS[0]}" > "$check_dir/priv_exit"
      echo "100"
      echo "# Privileged batch complete"
    ) | pipe_to_progress "UrStack (privileged)" 1 "$cancel_flag"
    ec=$(cat "$check_dir/priv_exit" 2>/dev/null || echo 1)
  else
    pkexec_priv "$jobs_file" 2>&1 | section_log priv | emit_lines
    ec=${PIPESTATUS[0]:-1}
  fi
  rm -f "$cancel_flag"
  return "$ec"
}

# priv.sh prints `#result <job> ok|fail` per job. The batch exit code is 1 if
# *any* job failed, so DNF used to show as failed whenever Cursor did.
priv_job_ec() {
  local job="$1"
  local logf="${RUN_LOG_DIR:-}/priv.log"
  local line=""
  [[ -n "${RUN_LOG_DIR:-}" && -f "$logf" ]] || return 1
  line=$(grep -E "^#result ${job}( |$)" "$logf" 2>/dev/null | tail -1) || true
  [[ "$line" == *" ok" ]] && return 0
  [[ "$line" == *" fail" ]] && return 1
  return 1
}

priv_section_ec() {
  local section="$1" fallback="${2:-1}"
  local logf="${RUN_LOG_DIR:-}/priv.log"
  if [[ -z "${RUN_LOG_DIR:-}" || ! -f "$logf" ]]; then
    echo "$fallback"
    return 0
  fi
  case "$section" in
    dnf)
      if grep -qE '^#result dnf_upgrade ' "$logf"; then
        priv_job_ec dnf_upgrade || { echo 1; return 0; }
        if grep -qE '^#result akmods_wait fail' "$logf"; then
          echo 1; return 0
        fi
        echo 0; return 0
      fi
      ;;
    snap)
      if grep -qE '^#result snap_refresh ' "$logf"; then
        priv_job_ec snap_refresh && echo 0 || echo 1
        return 0
      fi
      ;;
    fw)
      if grep -qE '^#result fwupd_update ' "$logf"; then
        priv_job_ec fwupd_update && echo 0 || echo 1
        return 0
      fi
      ;;
    cursor)
      if grep -qE '^#result cursor_rpm ' "$logf"; then
        priv_job_ec cursor_rpm && echo 0 || echo 1
        return 0
      fi
      if grep -qE '^#result cursor_dnf ' "$logf"; then
        priv_job_ec cursor_dnf && echo 0 || echo 1
        return 0
      fi
      ;;
  esac
  echo "$fallback"
}

apply_user_section() {
  local key="$1"
  local exit_code=0
  case "$key" in
    flatpak)
      echo "# Flatpak system update..."
      flatpak update -y --system 2>&1 | section_log flatpak | emit_lines
      local e1=${PIPESTATUS[0]}
      echo "# Flatpak user update..."
      flatpak update -y --user 2>&1 | section_log flatpak | emit_lines
      local e2=${PIPESTATUS[0]}
      [[ $e1 -eq 0 && $e2 -eq 0 ]] || exit_code=1
      ;;
    toolbox)
      local tb_name
      if [[ -f "$check_dir/toolbox_names" ]]; then
        while IFS= read -r tb_name; do
          [[ -z "$tb_name" ]] && continue
          echo "# Toolbx: upgrading $tb_name..."
          toolbox run -c "$tb_name" -- sudo dnf upgrade -y 2>&1 | section_log "toolbox-$tb_name" | emit_lines \
            || exit_code=1
        done < "$check_dir/toolbox_names"
      fi
      if [[ -f "$check_dir/distrobox_names" ]]; then
        while IFS= read -r tb_name; do
          [[ -z "$tb_name" ]] && continue
          echo "# Distrobox: upgrading $tb_name..."
          if ! distrobox enter -n "$tb_name" -- bash -lc \
              'if command -v dnf >/dev/null; then sudo dnf upgrade -y; elif command -v apt-get >/dev/null; then sudo apt-get update && sudo apt-get upgrade -y; else echo "No dnf/apt in container"; exit 1; fi' \
              2>&1 | section_log "distrobox-$tb_name" | emit_lines; then
            exit_code=1
          fi
        done < "$check_dir/distrobox_names"
      fi
      ;;
    npm)
      npm update -g 2>&1 | section_log npm | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    npm_user)
      npm update -g --prefix "$HOME/.local" 2>&1 | section_log npm_user | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    pip)
      local pip_ok=0 pip_failed="" p ex
      for p in $(python3 -m pip list --user --outdated 2>/dev/null | tail -n +3 | awk '{print $1}'); do
        echo "# Upgrading pip: $p..."
        python3 -m pip install --upgrade --user "$p" 2>&1 | section_log pip | emit_lines
        ex=${PIPESTATUS[0]}
        if [[ $ex -ne 0 ]]; then
          pip_ok=1
          pip_failed="${pip_failed:+$pip_failed }$p"
        fi
      done
      echo "pip:$pip_ok" >> "$results_file"
      [[ -n "$pip_failed" ]] && echo "pip_failed:$pip_failed" >> "$results_file"
      return 0
      ;;
    pipx)
      pipx upgrade-all 2>&1 | section_log pipx | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    rust)
      rustup update 2>&1 | section_log rust | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    cargo)
      cargo install-update -a 2>&1 | section_log cargo | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    node)
      local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
      local stream="node"
      stream=$(grep '^stream=' "$check_dir/node" 2>/dev/null | cut -d= -f2-) || true
      [[ -n "$stream" ]] || stream=$(cat "$nvm_dir/alias/default" 2>/dev/null || echo node)
      bash -l -c '
        export NVM_DIR="$1"
        # shellcheck disable=SC1091
        source "$1/nvm.sh" 2>/dev/null
        nvm install "$2" && nvm alias default "$2"
      ' _ "$nvm_dir" "$stream" 2>&1 | section_log node | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    claude)
      claude update 2>&1 | section_log claude | emit_lines
      exit_code=${PIPESTATUS[0]}
      ;;
    supabase)
      local sb_tmp sb_dir sb_asset
      sb_asset="$(supabase_release_asset)"
      sb_tmp=$(mktemp)
      sb_dir=$(mktemp -d)
      echo "# Downloading latest Supabase CLI ($sb_asset)..."
      if curl -fsSL -o "$sb_tmp" \
          "https://github.com/supabase/cli/releases/latest/download/${sb_asset}" \
        && tar -xzf "$sb_tmp" -C "$sb_dir" \
        && install -m 755 "$sb_dir/supabase" "$HOME/.local/bin/supabase"; then
        echo "# Installed $($HOME/.local/bin/supabase --version 2>/dev/null | head -1)"
        exit_code=0
      else
        echo "# Supabase CLI update failed"
        exit_code=1
      fi
      rm -rf "$sb_tmp" "$sb_dir"
      printf '%s\n' "$exit_code" | section_log supabase >/dev/null
      ;;
    dnf|snap|fw|cursor)
      # Handled in priv batch — mark from priv result
      return 0
      ;;
  esac
  echo "${key}:${exit_code}" >> "$results_file"
}

run_all_updates() {
  local selected="${1:-all}"
  results_file=$(mktemp)
  init_run_log

  local -a queue=()
  local s label
  for s in "${SECTION_KEYS[@]}"; do
    has_section "$s" || continue
    section_is_selected "$s" "$selected" || continue
    if [[ "$s" == "fw" ]] && skip_firmware "$selected"; then
      continue
    fi
    label=$(section_label "$s")
    queue+=("$s:$label")
  done

  local total_steps=$(( ${#queue[@]} + 1 ))
  [[ $total_steps -lt 1 ]] && total_steps=1
  local step_size=$(( 100 / total_steps ))
  local current_pct=0
  local priv_jobs priv_ec=0

  priv_jobs=$(build_priv_jobs "$selected")

  if [[ -n "$priv_jobs" ]]; then
    run_priv_jobs "$priv_jobs" || priv_ec=$?
    rm -f "$priv_jobs"
    # Mark priv sections from per-job #result lines when the log is present
    for s in dnf snap fw cursor; do
      has_section "$s" || continue
      section_is_selected "$s" "$selected" || continue
      if [[ "$s" == "fw" ]] && skip_firmware "$selected"; then
        continue
      fi
      echo "$s:$(priv_section_ec "$s" "$priv_ec")" >> "$results_file"
    done
    if has_section dnf && section_is_selected dnf "$selected"; then
      record_kernel_reboot_hint
      local hist
      hist=$(dnf_history_snippet || true)
      if [[ -n "$hist" ]]; then
        echo "dnf_history:${hist//$'\n'/ | }" >> "$results_file"
        printf '%s\n' "$hist" > "$RUN_LOG_DIR/dnf-history.txt"
      fi
    fi
    if has_section fw && section_is_selected fw "$selected" && ! skip_firmware "$selected" && [[ $priv_ec -eq 0 ]]; then
      echo "reboot_needed:1" >> "$results_file"
      echo "reboot_reason:Firmware update applied — reboot recommended." >> "$results_file"
    fi
  fi

  # Clean cursor rpm temp
  if [[ -f "$check_dir/cursor_rpm_tmp" ]]; then
    while IFS= read -r f; do rm -f "$f"; done < "$check_dir/cursor_rpm_tmp"
  fi

  local entry key lbl step_num=0
  if [[ "${AUTO_YES:-0}" -eq 0 && -n "${DISPLAY:-}" ]]; then
    (
      current_pct=20
      emit_progress "User-level updates..."
      for entry in "${queue[@]}"; do
        key="${entry%%:*}"
        lbl="${entry#*:}"
        case "$key" in dnf|snap|fw|cursor) continue ;; esac
        step_num=$((step_num + 1))
        emit_progress "[user $step_num] $lbl — running..."
        apply_user_section "$key"
        current_pct=$(( current_pct + step_size ))
        [[ $current_pct -gt 95 ]] && current_pct=95
        emit_progress "[user $step_num] $lbl — done."
      done
      echo "100"
      echo "# All updates complete!"
    ) | pipe_to_progress "UrStack" 0
  else
    for entry in "${queue[@]}"; do
      key="${entry%%:*}"
      lbl="${entry#*:}"
      case "$key" in dnf|snap|fw|cursor) continue ;; esac
      echo "=== $lbl ==="
      apply_user_section "$key"
    done
  fi

  record_smart_reboot_hint

  # ── Summary ──────────────────────────────────────────────────────────────
  local -a summary_parts=()
  local r pf pip_failed_str=""

  get_result() { grep "^$1:" "$results_file" 2>/dev/null | head -1 | cut -d: -f2- || true; }

  pf=$(get_result pip_failed)
  [[ -n "$pf" ]] && pip_failed_str=" (failed: $pf)"

  for s in "${SECTION_KEYS[@]}"; do
    label=$(section_label "$s")
    r=$(get_result "$s")
    [[ -z "$r" ]] && continue
    if [[ "$r" == "0" ]]; then
      summary_parts+=("• $label: ✓")
    else
      local extra=""
      [[ "$s" == "pip" ]] && extra="$pip_failed_str"
      summary_parts+=("• $label: ✗ failed$extra")
    fi
  done

  local hist_line
  hist_line=$(get_result dnf_history)
  if [[ -n "$hist_line" ]]; then
    summary_parts+=("• DNF history: ${hist_line:0:200}")
  fi

  local summary all_ok=1
  summary=$'Update summary:\n\n'"$(printf '%s\n' "${summary_parts[@]}")"
  [[ ${#summary_parts[@]} -gt 0 ]] && summary+=$'\n'

  for part in "${summary_parts[@]}"; do
    [[ "$part" == *"✗"* ]] && all_ok=0 && break
  done

  if [[ $all_ok -eq 1 ]]; then
    summary+=$'\n✓ All updates applied.'
    notify "Updates complete" "All updates applied successfully."
  else
    summary+=$'\n⚠ Some updates failed — see detailed logs.'
    notify "Updates finished with errors" "See $RUN_LOG_DIR" "critical"
  fi

  local reboot_reason=""
  reboot_reason=$(get_result reboot_reason)
  if [[ -n "$reboot_reason" ]]; then
    summary+=$'\n\n↺ Reboot recommended: '"$reboot_reason"
  fi
  if [[ -n "$RUN_LOG_DIR" ]]; then
    summary+=$'\n\nLogs: '"$RUN_LOG_DIR"
  fi

  printf '[%s]\n%s\n---\n' "$(date -Iseconds)" "$summary" >> "$LOG_FILE" 2>/dev/null || true
  printf '%s\n' "$summary" > "$RUN_LOG_DIR/summary.txt" 2>/dev/null || true

  if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
    echo "# Updates finished"
    echo "100"
    printf '%s\n' "$summary"
    # Reboot prompt stays out of embedded mode (shell shows toast/summary instead)
    return 0
  fi

  if [[ "${AUTO_YES:-0}" -eq 1 ]]; then
    echo -e "$summary"
  else
    local _ui="$FEDORA_UPDATES_LIB/ui.py"
    if [[ -n "${DISPLAY:-}" && -x "$_ui" ]] && command -v python3 &>/dev/null; then
      python3 "$_ui" message --type info --title "UrStack" --text "$summary" 2>/dev/null || true
    else
      zenity --info --title="UrStack" --text="$summary" --width=480 2>/dev/null || echo -e "$summary"
    fi
  fi

  prompt_reboot_if_needed
}

show_select_dialog() {
  local -a checklist_args=()
  local s label items_f selected
  items_f=$(mktemp)

  for s in "${SECTION_KEYS[@]}"; do
    has_section "$s" || continue
    label=$(section_label "$s")
    checklist_args+=(TRUE "$label")
    printf 'TRUE|%s|%s\n' "$s" "$label" >> "$items_f"
  done

  if [[ ! -s "$items_f" ]]; then
    rm -f "$items_f"
    run_all_updates "all"
    return
  fi

  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  selected=""
  if [[ -n "${DISPLAY:-}" && -x "$_ui" ]] && command -v python3 &>/dev/null; then
    selected=$(python3 "$_ui" checklist \
      --title "Apply updates" \
      --text "Select which updates to apply:" \
      --items-file "$items_f" \
      --ok-label "Apply" 2>/dev/null) || selected=""
  else
    selected=$(zenity --list --checklist --title="UrStack" \
      --text="Select which updates to apply:" \
      --column="" --column="Section" --separator="|" \
      "${checklist_args[@]}" 2>/dev/null) || true
  fi
  rm -f "$items_f"
  [[ -n "$selected" ]] && run_all_updates "$selected"
}

show_log_viewer() {
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  if [[ -f "$LOG_FILE" ]]; then
    if [[ -n "${DISPLAY:-}" && -x "$_ui" ]] && command -v python3 &>/dev/null; then
      python3 "$_ui" text --title "UrStack — History" --file "$LOG_FILE" --ok-label "Back" 2>/dev/null || true
    else
      zenity --text-info --title="UrStack — History" \
        --filename="$LOG_FILE" --width=800 --height=620 --ok-label="Back" 2>/dev/null || true
    fi
  else
    if [[ -n "${DISPLAY:-}" && -x "$_ui" ]]; then
      python3 "$_ui" message --type info --title "UrStack" --text "No update history found yet." 2>/dev/null || true
    else
      zenity --info --title="UrStack" --text="No update history found yet." 2>/dev/null || true
    fi
  fi
}

show_runs_browser() {
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  local runs_dir="$LOG_DIR/runs"
  if [[ -n "${DISPLAY:-}" && -x "$_ui" ]] && command -v python3 &>/dev/null; then
    python3 "$_ui" runs --title "UrStack — Run logs" --runs-dir "$runs_dir" 2>/dev/null || true
  else
    if [[ -d "$runs_dir" ]]; then
      ls -1t "$runs_dir" | head -n 20
      echo "Open a folder under: $runs_dir"
    else
      echo "No run logs yet."
    fi
  fi
}

show_catalog() {
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  local status_f choice
  status_f=$(mktemp)

  while true; do
    catalog_status_file "$status_f" || true
    choice=""
    if [[ -n "${DISPLAY:-}" && -f "$_ui" ]] && command -v python3 &>/dev/null; then
      choice=$(python3 "$_ui" catalog \
        --title "${APP_NAME:-UrStack} — Apps" \
        --status-file "$status_f" 2>/dev/null) || true
      choice=$(printf '%s' "$choice" | tr -d '\r' | awk 'NF{print; exit}')
    else
      echo "App catalog needs the GUI. Edit data/catalog/apps.json or install Flatpak apps manually."
      rm -f "$status_f"
      return 0
    fi

    if catalog_consume_choice "$choice" "$status_f"; then
      continue
    fi
    break
  done
  rm -f "$status_f"
}

# Handle install| / install-batch| results from catalog or shell UI.
# Returns 0 if the choice was an install action (caller should refresh UI).
catalog_consume_choice() {
  local choice="$1"
  local status_f="$2"
  local _ui="$FEDORA_UPDATES_LIB/ui.py"

  case "$choice" in
    'install-batch|'*)
      local batch_f method package name url
      batch_f="${choice#install-batch|}"
      if [[ ! -f "$batch_f" ]]; then
        [[ -f "$_ui" ]] && python3 "$_ui" message --type error --title "$APP_NAME" \
          --text "Install list missing." 2>/dev/null || true
        return 0
      fi
      local total ok fail opened
      total=$(grep -cve '^[[:space:]]*$' "$batch_f" 2>/dev/null || echo 0)
      (
        local i=0 pct denom method package name url
        local ok=0 fail=0 opened=0
        denom=$total
        [[ "${denom:-0}" -lt 1 ]] && denom=1
        while IFS='|' read -r method package name url; do
          [[ -n "$method" ]] || continue
          i=$((i + 1))
          pct=$(( (i - 1) * 100 / denom ))
          echo "$pct"
          echo "# ($i/$total) Installing $name..."
          if catalog_install_app "$method" "$package" "$name" "$url"; then
            if [[ "$method" == "browser" ]]; then
              opened=$((opened + 1))
              echo "# Opened page for $name"
            else
              ok=$((ok + 1))
              echo "# Installed $name"
            fi
          else
            fail=$((fail + 1))
            echo "# Failed: $name"
          fi
        done < "$batch_f"
        echo "100"
        echo "# Done — installed $ok, opened $opened, failed $fail"
        printf '%s\n' "$ok|$fail|$opened|$total" > "${status_f}.ec"
      ) | pipe_to_progress "UrStack — Install selected ($total)" 0
      local summary
      summary=$(cat "${status_f}.ec" 2>/dev/null || echo "0|1|0|0")
      rm -f "${status_f}.ec" "$batch_f"
      IFS='|' read -r ok fail opened total <<< "$summary"
      if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
        echo "# Finished $total app(s) — installed ${ok:-0}, opened ${opened:-0}, failed ${fail:-0}"
        return 0
      fi
      if [[ -f "$_ui" ]]; then
        if [[ "${fail:-1}" == "0" ]]; then
          python3 "$_ui" message --type info --title "$APP_NAME" \
            --text "Finished $total app(s)."$'\n\n'"Installed: ${ok:-0}"$'\n'"Opened download page: ${opened:-0}" 2>/dev/null || true
        else
          python3 "$_ui" message --type error --title "$APP_NAME" \
            --text "Finished with errors."$'\n\n'"Installed: ${ok:-0}"$'\n'"Opened pages: ${opened:-0}"$'\n'"Failed: ${fail:-0}" 2>/dev/null || true
        fi
      fi
      return 0
      ;;
    'install|'*)
      local method package name url
      IFS='|' read -r _ method package name url <<< "$choice"
      (
        echo "5"
        echo "# Preparing to install $name..."
        catalog_install_app "$method" "$package" "$name" "$url"
        ec=$?
        if [[ $ec -eq 0 ]]; then
          echo "100"
          if [[ "$method" == "browser" ]]; then
            echo "# Opened download page for $name"
          else
            echo "# Installed $name"
          fi
        else
          echo "100"
          echo "# Failed to install $name"
        fi
        echo "$ec" > "${status_f}.ec"
      ) | pipe_to_progress "UrStack — Install $name" 0
      local ec
      ec=$(cat "${status_f}.ec" 2>/dev/null || echo 1)
      rm -f "${status_f}.ec"
      if [[ "${URSTACK_EMBEDDED_PROGRESS:-0}" == "1" ]]; then
        return 0
      fi
      if [[ -f "$_ui" ]]; then
        if [[ "$ec" == "0" ]]; then
          local msg="Installed $name."
          [[ "$method" == "browser" ]] && msg="Opened the download page for $name."$'\n\n'"Complete the vendor installer, then reopen Apps to refresh status."
          python3 "$_ui" message --type info --title "$APP_NAME" \
            --text "$msg" 2>/dev/null || true
        else
          python3 "$_ui" message --type error --title "$APP_NAME" \
            --text "Could not install $name."$'\n\n'"Method: $method"$'\n'"Package: $package" 2>/dev/null || true
        fi
      fi
      return 0
      ;;
    *) return 1 ;;
  esac
}

show_settings() {
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  local choice=""
  while true; do
    if [[ -n "${DISPLAY:-}" && -f "$_ui" ]] && command -v python3 &>/dev/null; then
      choice=$(python3 "$_ui" settings \
        --title "${APP_NAME:-UrStack} — Settings" \
        --config-file "$FEDORA_UPDATES_USER_CONFIG" 2>/dev/null) || true
      choice=$(printf '%s' "$choice" | tr -d '\r' | awk 'NF{print; exit}')
    else
      echo "Edit config: $FEDORA_UPDATES_USER_CONFIG"
      echo "Or run: urstack --detect --write-config"
      return 0
    fi
    case "$choice" in
      saved)
        load_updater_config
        ;;
      rescan)
        print_detection_report
        write_detected_config "$FEDORA_UPDATES_USER_CONFIG" 0
        load_updater_config
        # reopen settings with new values (legacy path; in-app scan is preferred)
        continue
        ;;
      *) return 0 ;;
    esac
    return 0
  done
}

show_action_menu() {
  local has_updates="${1:-0}"
  local results_pane="${2:-}"
  local choice
  local _ui="$FEDORA_UPDATES_LIB/ui.py"
  local enable_backup="${ENABLE_BACKUP:-0}"
  local status_f start_page="" sections_f
  status_f=$(mktemp)
  sections_f=$(mktemp)
  local runs_dir="${XDG_STATE_HOME:-$HOME/.local/state}/urstack/runs"

  while true; do
    choice=""
    catalog_status_file "$status_f" || true
    : > "$sections_f"
    write_apply_sections_file "$sections_f"
    if [[ -f "$_ui" ]] && command -v python3 &>/dev/null; then
      choice=$(python3 "$_ui" shell \
        --file "$results_pane" \
        --has-updates "$has_updates" \
        --enable-backup "$enable_backup" \
        --title "${APP_NAME:-UrStack}" \
        --status-file "$status_f" \
        --config-file "$FEDORA_UPDATES_USER_CONFIG" \
        --log-file "$LOG_FILE" \
        --runs-dir "$runs_dir" \
        --sections-file "$sections_f" \
        --check-dir "$check_dir" \
        --pending-check "${URSTACK_PENDING_CHECK:-0}" \
        --start-page "$start_page" 2>/dev/null) || true
      choice=$(printf '%s' "$choice" | tr -d '\r' | awk 'NF{print; exit}')
      start_page=""
      URSTACK_PENDING_CHECK=0
      # After the first in-window check, keep has_updates in sync if shell wrote results
      if [[ -f "$results_pane" ]]; then
        if grep -q '^=== ' "$results_pane" 2>/dev/null; then
          has_updates=1
        elif grep -qi 'nothing to update' "$results_pane" 2>/dev/null; then
          has_updates=0
        fi
      fi
    else
      local -a rows=()
      [[ "$has_updates" == "1" ]] && rows+=("apply" "Apply updates")
      rows+=("backup" "Backup setup")
      rows+=("restore" "Restore setup")
      rows+=("apps" "Browse apps")
      rows+=("settings" "Settings")
      rows+=("log" "View log")
      rows+=("runs" "Browse run logs")
      choice=$(zenity --list --title="${APP_NAME:-UrStack}" \
        --text="Choose an action:" \
        --column="id" --column="Action" --hide-column=1 \
        --width=520 --height=380 \
        --ok-label="Select" --cancel-label="Close" \
        "${rows[@]}" 2>/dev/null) || true
      choice="${choice%%|*}"
    fi

    case "$choice" in
      apply) show_select_dialog; break ;;
      backup)
        fedora_setup_backup_ui
        ;;
      restore)
        fedora_setup_restore_ui
        ;;
      'backup|'*)
        _payload="${choice#backup|}"
        _parent="${_payload%%|*}"
        _opts=""
        if [[ "$_payload" == *"|"* ]]; then
          _opts="${_payload#*|}"
        fi
        URSTACK_BACKUP_OPTS="$_opts" fedora_setup_backup_to "$_parent" >/dev/null || true
        ;;
      'restore|'*)
        _payload="${choice#restore|}"
        _dest="${_payload%%|*}"
        _opts=""
        if [[ "$_payload" == *"|"* ]]; then
          _opts="${_payload#*|}"
        fi
        URSTACK_BACKUP_OPTS="$_opts" fedora_setup_restore_from "$_dest" || true
        ;;
      apps) show_catalog ;;
      settings) show_settings ;;
      log) show_log_viewer ;;
      runs) show_runs_browser ;;
      rescan)
        print_detection_report
        write_detected_config "$FEDORA_UPDATES_USER_CONFIG" 0
        load_updater_config
        # Stay in Settings — no separate message window (scan now runs in-app).
        start_page=settings
        ;;
      *)
        if catalog_consume_choice "$choice" "$status_f"; then
          start_page=apps
          continue
        fi
        break
        ;;
    esac
  done
  rm -f "$status_f" "$sections_f"
}

build_results_message() {
  sections=()
  advisory_sections=()
  has_any=0

  if has_section preflight_errors; then
    advisory_sections+=("=== Preflight problems ==="$'\n'"$(read_check preflight_errors)")
  fi
  if has_section preflight_advisories; then
    advisory_sections+=("=== Preflight notes ==="$'\n'"$(read_check preflight_advisories)")
  fi
  if has_section dnf; then
    sections+=("=== DNF ($(read_check dnf_count) package(s)) ==="$'\n'"$(read_check dnf)")
    has_any=1
  fi
  if has_section snap; then
    sections+=("=== Snap ==="$'\n'"$(read_check snap)"); has_any=1
  fi
  if has_section fw; then
    sections+=("=== Firmware ==="$'\n'"$(read_check fw)"); has_any=1
  fi
  if has_section flatpak; then
    sections+=("=== Flatpak ($(read_check flatpak_count) update(s)) ==="$'\n'"$(read_check flatpak)")
    has_any=1
  fi
  if has_section toolbox; then
    sections+=("=== Toolbx / Distrobox ==="$'\n'"$(read_check toolbox)"); has_any=1
  fi
  if has_section dnf_changelog; then
    advisory_sections+=("=== DNF changelog snippets ==="$'\n'"$(read_check dnf_changelog)")
  fi
  if has_section jetbrains_advisory; then
    advisory_sections+=("=== JetBrains Toolbox ==="$'\n'"$(read_check jetbrains_advisory)")
  fi
  if has_section npm; then
    sections+=("=== npm global (nvm) ==="$'\n'"$(read_check npm)"); has_any=1
  fi
  if has_section npm_user; then
    sections+=("=== npm user (~/.local) ==="$'\n'"$(read_check npm_user)"); has_any=1
  fi
  if has_section pip; then
    sections+=("=== pip packages ==="$'\n'"$(read_check pip)"); has_any=1
  fi
  if has_section pipx; then
    sections+=("=== pipx ==="$'\n'"$(read_check pipx)"); has_any=1
  fi
  if has_section rust; then
    sections+=("=== rustup ==="$'\n'"$(read_check rust)"); has_any=1
  fi
  if has_section cargo; then
    sections+=("=== Cargo binaries ==="$'\n'"$(read_check cargo)"); has_any=1
  fi
  if has_section node; then
    local _node_info _node_current _node_latest
    _node_info=$(read_check node)
    _node_current=$(echo "$_node_info" | grep '^current=' | cut -d= -f2)
    _node_latest=$(echo "$_node_info" | grep '^latest=' | cut -d= -f2)
    sections+=("=== Node.js (nvm) ==="$'\n'"Current: $_node_current"$'\n'"Latest:  $_node_latest")
    has_any=1
  fi
  if has_section cursor; then
    sections+=("=== Cursor AI ==="$'\n'"$(read_check cursor)"); has_any=1
  fi
  if has_section claude; then
    sections+=("=== Claude Code ==="$'\n'"$(read_check claude)"); has_any=1
  fi
  if has_section supabase; then
    sections+=("=== Supabase CLI ==="$'\n'"$(read_check supabase)"); has_any=1
  fi
  if has_section flatpak_eol; then
    advisory_sections+=("=== Flatpak OCI notes ==="$'\n'"$(read_check flatpak_eol)")
  fi
  if has_section appimage_advisory; then
    advisory_sections+=("=== AppImages ==="$'\n'"$(read_check appimage_advisory)")
  fi

  if [[ $has_any -eq 1 ]]; then
    msg=$(printf '%s\n\n' "${sections[@]}")
  else
    msg=$'Nothing to update.\n\nChecked: DNF, Snap, Firmware, Flatpak, Toolbx, npm, pip, pipx, rustup, Cargo, Node.js, Cursor, Claude Code, Supabase CLI'
    msg+=$'\n\nTip: cargo needs "cargo install cargo-update" for binary update checks.'
  fi
  if [[ ${#advisory_sections[@]} -gt 0 ]]; then
    msg+=$'\n\n'"$(printf '%s\n\n' "${advisory_sections[@]}")"
  fi
}
