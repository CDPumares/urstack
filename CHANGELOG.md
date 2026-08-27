# Changelog

All notable changes to this project are listed here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Look page: pack the live wallpaper, custom icons, cursors, widgets, and theme; browse a curated GitHub catalog of community palettes (Dracula, Nord, Catppuccin, Sweet, Bibata) instead of the GNOME/KDE store; Install copies the pack into your home and switches this desktop to it. Overview is a 3×3 card grid so Look sits with the other sections.

## [0.3.0] — 2026-08-27

Tray, startup options, a calmer desktop shell, and a round of correctness and safety fixes.

### Added

- Grey system-tray indicator while the window is open (StatusNotifierItem on Plasma, Cinnamon, XFCE, COSMIC; a small dash window on GNOME). Right-click opens Check, Updates, Apps, Health, Backup, Restore, Settings, and Quit.
- `--check --tray` for a silent scan with the same icon (used by login autostart in background mode).
- Launch at login, optional background check at login, scan-on-startup, and a daily user systemd timer that only checks and notifies.
- My apps overlay (`catalog-user.json`) so personal listings survive a rebuild.
- Desktop screenshots in the README (Overview, Apps, Health, Backup).

### Changed

- Overview is a two-row card grid with a pinned footer; sidebar can collapse.
- Backup size is recorded in `last-backup.conf` instead of walking the tree on the GTK thread.
- Secrets (SSH, GPG, git credentials, GitHub CLI, KDE Wallet) are **off by default**. The only one-click include is the labelled *Everything + secrets* preset.
- Health restore points re-enable user units they turned off, and the UI/README say what a restore cannot rewind (emptied caches, trash, unrelated packages).
- Cursor catalog RPM fallback follows `uname -m` instead of always fetching `linux-x64`.

### Fixed

- System install no longer deletes the PolicyKit policy it just wrote.
- Pre-restore undo directory fails closed instead of overwriting `$HOME` with no way back.
- Firefox `places.sqlite` is copied aside before a restore overwrites bookmarks.
- Wallpaper and icon paths with spaces are backed up instead of silently skipped.
- Privileged `/etc` drop-ins are installed as `root:root` `0644`; the jobs file is snapshotted after validation.
- Tray no longer uses Plasma `NeedsAttention` (that made the icon pulse and swap in the colour app logo). After Apply, the tray is told idle when nothing remains.
- CI: shellcheck redirection, GTK tests on the system Python that has `gi`, and a shared `ui.py` module name so GObject types are not registered twice.

## [0.2.0] — 2026-08-21

Rename, catalog, and backup that can target a desktop.

### Added

- CLIs & tools catalog category, with more command-line listings.
- KDE / GNOME desktop presets for backup and restore.
- Roadmap for ports beyond Fedora.
- [CONTRIBUTING.md](CONTRIBUTING.md) and an [AI use](README.md#ai-use) note.

### Changed

- The project is **UrStack** (`urstack`). `stackup` and `fedora-updates` remain compatibility commands.
- User config and logs move to `~/.config/urstack/` and `~/.local/state/urstack/` (legacy directories are copied once).
- Backup privileged work goes through the batched PolicyKit helper.
- Windows-only catalog entries removed; firmware apply is opt-in (`apply_fw`).

## [0.1.0] — 2026-08-21

First public release.

### Added

- GTK4 / libadwaita desktop app and a CLI that share the same checks and apply path.
- Parallel update checks: DNF, Flatpak, Snap, firmware, and optional developer toolchains.
- Apps catalog with descriptions, screenshots, and Fedora-appropriate install methods.
- Health scan with optional restore points before aggressive fixes.
- Backup / restore as a workstation blueprint (manifests, settings, projects) rather than a disk image.
- User and system installer, PolicyKit helper, MIT license.

[Unreleased]: https://github.com/CDPumares/urstack/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/CDPumares/urstack/releases/tag/v0.3.0
[0.2.0]: https://github.com/CDPumares/urstack/releases/tag/v0.2.0
[0.1.0]: https://github.com/CDPumares/urstack/releases/tag/v0.1.0
