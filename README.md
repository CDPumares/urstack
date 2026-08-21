<p align="center">
  <img src="assets/urstack.png" alt="UrStack logo" width="128" height="128">
</p>

<h1 align="center">UrStack</h1>

<p align="center">
  <strong>Your Fedora workstation, under one roof.</strong>
</p>

<p align="center">
  <a href="#features">Features</a> ·
  <a href="#requirements">Requirements</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#the-desktop-app">Desktop App</a> ·
  <a href="#cli-usage">CLI</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#project-layout">Layout</a> ·
  <a href="#contributing">Contributing</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/OS-Fedora_Linux-blue?logo=fedora" alt="Fedora">
  <img src="https://img.shields.io/badge/GUI-GTK4%2Flibadwaita-3584e4?logo=gnome" alt="GTK4 / libadwaita">
  <img src="https://github.com/CDPumares/urstack/actions/workflows/ci.yml/badge.svg" alt="CI">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License">
</p>

---

**UrStack** is a native GTK4 / libadwaita companion for daily-driver Fedora machines. It checks and applies updates across package managers and developer toolchains, installs popular software from a curated catalog, tunes workstation health, and backs up a machine so you can rebuild it later — from one dashboard, with PolicyKit for privileged steps instead of pasted `sudo` commands.

Launch it as `urstack` or `stackup` (the `fedora-updates` command is kept as a compatibility alias).

---

## Features

### Universal update engine

UrStack aggregates pending updates in parallel and lets you apply them from one place:

- **Core sources:** DNF (dnf / dnf5), Flatpak, Snap, and firmware via `fwupd`.
- **Developer toolchains** (enabled when detected, or from Settings): nvm / npm, pip / pipx, rustup / cargo, Toolbx / Distrobox, Node.js, Cursor, Claude Code, and Supabase CLI.
- **Advisories** (reminders, not auto-applied): JetBrains Toolbox and AppImages under `~/Applications`.
- **PolicyKit:** elevated DNF, Snap, firmware, and similar steps use the native authentication dialog.
- **Play well with other updaters:** optional quieting of GNOME Software while UrStack runs, and DNF excludes for Plasma Discover if you removed it.

Firmware apply is skipped in non-interactive `--yes` mode unless you pass `--include-firmware`.

### Curated app catalog

The Apps page is a category browser, not a dump of every Flathub listing:

- Categories include browsers, communication, media, productivity, developer tools, graphics, utilities, gaming, and vendor / direct downloads.
- Install methods are mapped per app: Flatpak (Flathub), DNF, Snap, or a vendor URL / AppImage when that is the realistic Linux path.
- Linux-mapped profiles inspired by [Chris Titus Tech’s winutil](https://github.com/ChrisTitusTech/winutil) sit alongside the native catalog for one-click batch installs.
- Filters for installed vs available, plus bundled app logos from the catalog.

### Workstation health and tuning

The Health page scans the machine and offers optional, selectable actions:

| Area | Examples |
| --- | --- |
| Cleanup | Old kernels, DNF cache, journal vacuum, unused Flatpak runtimes, orphan `~/.var/app` data |
| Workstation | Flathub remote, RPM Fusion, multimedia codecs |
| Memory | zram-generator, EarlyOOM |
| Power | `power-profiles-daemon` profiles; TLP vs ppd conflict |
| Advanced | `fstrim`, DNF parallel downloads, sysctl drop-in (`/etc/sysctl.d/99-urstack.conf`) |

Aggressive tweaks take a restore point first (see [Safety nets](#safety-nets)).

### Safety nets

Before applying health actions, UrStack records a restore point under `~/.local/state/stackup/health-restore-points/`: DNF state, selected unit enablement, and UrStack sysctl drop-ins. You can list, create, and apply restore points from the Health page or the CLI.

### Backup and rebuild

Backup is a **blueprint**, not a full-disk image. A dated folder can include:

- Package and CLI manifests (DNF user packages, Flatpak, Snap, npm, pip, pipx, cargo, rustup, nvm, PATH inventory)
- Git repositories under configured project roots (build artefacts such as `node_modules` and `.venv` are skipped)
- AppImages and vendor launchers
- Desktop settings, themes, SSH / GPG material (opt-in), and browser profiles
- Hardware / driver inventory so restore can distinguish same-PC vs different-GPU machines

Restore reinstalls from those manifests and overlays settings. Enable the module in Settings or with `--include-backup` when writing a detected config.

---

## Requirements

- **OS:** Fedora Workstation, or another Fedora spin with a GTK4 / libadwaita session (GNOME, KDE Plasma with libadwaita, and similar).
- **Runtime:** Python 3, GTK 4, libadwaita. Zenity is an optional fallback for some dialogs if the GTK UI cannot start.
- **Privileges:** user install needs no root. System install and privileged updates use `sudo` / PolicyKit.

rpm-ostree / Silverblue-style immutable Fedora is not the target; UrStack expects a classic DNF workstation.

---

## Quick Start

Clone the repository (or open the project directory) and run the installer:

```bash
git clone https://github.com/CDPumares/urstack.git
cd urstack
./install.sh --user
```

That copies the app under `~/.local/share/stackup`, puts `urstack` / `stackup` on `~/.local/bin`, installs desktop entries and icons, and writes `~/.config/stackup/config.conf` if it does not already exist.

On first install with the default **auto** profile, the installer scans the workstation and enables only the sources it finds (DNF, Flatpak, Snap, fwupd, plus any toolchains present). Existing config is never overwritten.

Then launch from the app menu, or:

```bash
urstack
# or
stackup
```

The installer is meant to be run from a terminal (`./install.sh --user`). A local `Install UrStack.desktop` with an absolute `Icon=` path is machine-specific and is not in git.

### Install modes

```text
./install.sh [--user|--system] [--profile auto|default|developer] [--uninstall]
```

| Option | Effect |
| --- | --- |
| `--user` | Current user only (default). Files under `~/.local`. |
| `--system` | `/usr/local` plus PolicyKit policy (needs sudo). |
| `--profile auto` | Scan this machine and write config (default when config is missing). |
| `--profile default` | Core sources only (DNF, Flatpak, Snap, firmware). |
| `--profile developer` | All plugins on, including backup. |
| `--uninstall` | Remove installed files for the chosen mode. Config and logs are kept. |

After a user install, ensure `~/.local/bin` is on your `PATH`.

### Uninstall

```bash
./install.sh --user --uninstall
# or
./install.sh --system --uninstall
```

Config stays at `~/.config/stackup/config.conf`. Logs stay under `~/.local/state/stackup`.

---

## The desktop app

The main window is a sidebar shell:

| Page | Role |
| --- | --- |
| **Overview** | Snapshot of updates, health score, and recent history. |
| **Updates** | Parallel check, per-source cards, apply selected or all. |
| **Apps** | Category catalog, filters, single or batch install. |
| **Health** | Scan, pick actions, apply; restore points. |
| **Backup** / **Restore** | Blueprint export and rebuild. |
| **Settings** | Toggle sources, kernel keep-count, re-scan workstation. |
| **History** / **Runs** | Combined log and per-run folders. |

Checks run in the background after launch. You can open Apps, Backup, Settings, and History while a scan is still finishing.

---

## CLI usage

With no flags, UrStack opens the GUI (and detaches from the terminal). Flags stay in the foreground.

```text
urstack                         # GUI
urstack --check                 # Print results; exit 1 if anything is pending
urstack --yes                   # Non-interactive apply (skips firmware)
urstack --yes --include-firmware
urstack --log                   # History viewer
urstack --config                # Config path, enabled sources, dump file
urstack --detect                # Scan workstation; print what would be enabled
urstack --detect --write-config
urstack --detect --write-config --include-backup
urstack --backup [dir]
urstack --restore [dir]
urstack --install-timer         # Daily user systemd timer (check only)
urstack --health-scan --health-status <file>
urstack --health-apply <id,id,…>
urstack --health-restore-point
urstack --health-restore-list
urstack --health-restore [id|latest]
```

`--check` is suitable for scripts and the daily timer: exit `0` if the stack is current, `1` if updates exist, `3` if another instance holds the lock.

### Daily check timer

```bash
urstack --install-timer
```

Installs a user unit `stackup-check.timer` (`OnCalendar=daily`, randomized delay). It only **checks** and notifies; it does not apply updates.

---

## Configuration

User config (created on first run / install):

```text
~/.config/stackup/config.conf
```

Keys are `1` / `0`. Shipped templates live in `config/default.conf` and `config/developer.conf`. Settings in the GUI write the same file (with a timestamped `.bak-*` copy).

### Core

| Key | Default | Meaning |
| --- | --- | --- |
| `enable_dnf` | 1 | RPM updates |
| `enable_flatpak` | 1 | Flatpak apps and runtimes |
| `enable_snap` | 1 | Snap refresh |
| `enable_fw` | 1 | fwupd |
| `enable_kernel_prune` | 1 | Drop old kernels after DNF (see `keep_kernels`) |
| `exclude_discover` | 1 | DNF `--exclude` for Plasma Discover packages |
| `quiet_gnome_software` | 1 | Pause GNOME Software’s background service during a GUI run |
| `keep_kernels` | 3 | Kernels to keep when pruning |

### Plugins

| Key | Default | Meaning |
| --- | --- | --- |
| `enable_toolbox` | 0 | Toolbx / Distrobox |
| `enable_npm` / `enable_npm_user` | 0 | nvm global npm / `~/.local` npm |
| `enable_pip` / `enable_pipx` | 0 | pip `--user` / pipx |
| `enable_rust` / `enable_cargo` | 0 | rustup / `cargo install` |
| `enable_node` | 0 | nvm Node version vs latest |
| `enable_cursor` / `enable_claude` / `enable_supabase` | 0 | Editor / CLI updaters |
| `enable_jetbrains` / `enable_appimage` | 0 | Advisories only |
| `enable_backup` | 0 | Backup / restore module |

### Backup paths

| Key | Default | Meaning |
| --- | --- | --- |
| `backup_project_roots` | `Documents:Projects:src:Desktop:waydroid_script` | Colon- or comma-separated; relative to `$HOME` or absolute |
| `backup_project_depth` | 3 | How deep to search each root for git repos (1–6) |
| `backup_full_dotconfig` | 1 | Also rsync most of `~/.config` and `~/.local/share` (caches excluded) |

Re-scan and rewrite config (backs up the previous file):

```bash
urstack --detect --write-config
```

Legacy `~/.config/fedora-workstation-updater/` is copied to `~/.config/stackup/` once if the new directory does not exist.

### Runtime paths

| What | Where |
| --- | --- |
| User config | `~/.config/stackup/config.conf` |
| Logs | `~/.local/state/stackup/` (`stackup.log` and per-run folders) |
| Health restore points | `~/.local/state/stackup/health-restore-points/` |
| User install | `~/.local/share/stackup/` |
| System install | `/usr/local/share/stackup/` |
| Lock | `$XDG_RUNTIME_DIR/stackup-$UID.lock` |

---

## Project layout

```text
bin/stackup              Entry point (GUI + CLI)
bin/fedora-updates       Compatibility wrapper
install.sh               User / system installer
config/                  default.conf, developer.conf
data/catalog/            apps.json, winutil.json, icon-map.json, metadata.json, icons/
data/icons/              Application icons (hicolor)
data/polkit/             PolicyKit policy (system install)
lib/core/                checks, apply, catalog, detect, priv, GTK UI
lib/plugins/             health.sh, backup.sh
scripts/                 Catalog / icon-map helpers
tests/                   Parser unit tests
packaging/NOTES.md       RPM / Flatpak notes
```

Privileged work is batched through `lib/core/priv.sh` (pkexec). The GTK UI is `lib/core/ui.py`.

Flatpak packaging is a poor fit: DNF and fwupd need host privileges. An RPM (for example via Copr) wrapping this tree is the intended distribution path; see `packaging/NOTES.md`.

---

## Contributing

Bug reports, catalog additions, and patches are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, tests, and the PolicyKit rules.

- [Open an issue](https://github.com/CDPumares/urstack/issues/new/choose)
- Keep privileged operations in `lib/core/priv.sh`
- Do not add `auth_admin_keep` to PolicyKit policy

Parser tests (need GTK / libadwaita on the machine):

```bash
python3 -m unittest tests.test_parsers
```

Catalog maintenance helpers:

```bash
python3 scripts/import-winutil-apps.py
python3 scripts/build-icon-map.py
python3 scripts/vendor-app-icons.py
python3 scripts/vendor-app-metadata.py
```

`vendor-app-metadata.py` pulls Flathub AppStream (description, developer, license, links, screenshot URLs) into `data/catalog/metadata.json`. Screenshot images are cached on first view under `~/.cache/stackup/app-meta/`.

---

## License

MIT. Use, fork, and adapt freely.
