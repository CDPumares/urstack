#!/usr/bin/env python3
"""Make a Plasma overlay usable after restore onto a different user/activity.

Wallpaper, launcher icons, Kickoff/Andromeda favorites and desktop widgets all
embed absolute paths or activity UUIDs. A restore that only rsyncs files leaves
those pointing at the old machine, so Plasma creates a blank default desktop.

Blueprints also replace $HOME with a token so the archive itself has no
username — any account on any machine can restore it.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import sys
from pathlib import Path

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HOME_RE = re.compile(r"(?:file://)?(/home/[^/\s\"']+)")
_MEDIA_KEY_RE = re.compile(
    r"^(Image|PreviewImage|customButtonImage|usersWallpapers|SlidePaths)=(.*)$",
    re.MULTILINE,
)

PORTABLE_WALLPAPER_REL = Path(".local/share/wallpapers/urstack")

# Named scheme written on restore so Plasma/GTK use the overlay colours
# instead of leftover Breeze Dark (grey) or a look-and-feel package.
RESTORED_COLOR_SCHEME = "UrStackRestored"

_COLOR_SECTIONS = (
    "ColorEffects:Disabled",
    "ColorEffects:Inactive",
    "Colors:Button",
    "Colors:Complementary",
    "Colors:Header",
    "Colors:Header][Inactive",
    "Colors:Selection",
    "Colors:Tooltip",
    "Colors:View",
    "Colors:Window",
    "WM",
)

# Stand-in for $HOME inside a blueprint so the archive has no username in it.
# Restore expands this to the account that is actually logging in.
HOME_TOKEN = "__URSTACK_HOME__"

_SKIP_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    "Cache",
    "CacheStorage",
    "Code Cache",
    "GPUCache",
    "Crashpad",
    ".ssh",
    ".gnupg",
    "kwalletd",
}

_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".ico",
    ".icns",
    ".svgz",
    ".mp3",
    ".mp4",
    ".mkv",
    ".webm",
    ".wav",
    ".flac",
    ".ogg",
    ".pdf",
    ".zip",
    ".7z",
    ".gz",
    ".xz",
    ".bz2",
    ".zst",
    ".tar",
    ".rpm",
    ".deb",
    ".appimage",
    ".so",
    ".o",
    ".a",
    ".pyc",
    ".pyo",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".kdbx",
    ".gpg",
    ".kbx",
    ".pem",
    ".der",
    ".p12",
    ".pfx",
    ".key",
}

_MAX_REWRITE_BYTES = 8 * 1024 * 1024

# Text configs that embed /home/<user>. Places/bookmarks are what Dolphin and
# the file dialogs show as Documents / Pictures / … — miss these and every
# sidebar entry says the old user does not exist.
_PATH_GLOBS = (
    ".config/plasma-org.kde.plasma.desktop-appletsrc",
    ".config/plasmashellrc",
    ".config/plasmarc",
    ".config/kscreenlockerrc",
    ".config/kactivitymanagerdrc",
    ".config/kactivitymanagerd-statsrc",
    ".config/kactivitymanagerd-pluginsrc",
    ".config/kactivitymanagerd-switcherrc",
    ".config/kdeglobals",
    ".config/dolphinrc",
    ".config/kfiledialogsrc",
    ".config/gtk-3.0/bookmarks",
    ".config/gtk-4.0/bookmarks",
    ".config/kdedefaults/kdeglobals",
    ".config/kdedefaults/plasmarc",
    ".config/user-dirs.dirs",
    ".local/share/user-places.xbel",
    ".local/share/user-places.xbel.bak",
    ".local/share/recently-used.xbel",
)

# Activity UUIDs only. Places files also contain disk UUIDs — remapping those
# would rewrite the wrong identifiers.
_ACTIVITY_GLOBS = (
    ".config/plasma-org.kde.plasma.desktop-appletsrc",
    ".config/plasmashellrc",
    ".config/kscreenlockerrc",
    ".config/kactivitymanagerd-statsrc",
    ".config/kactivitymanagerd-pluginsrc",
    ".config/kactivitymanagerd-switcherrc",
)

# Keep the old name for callers/tests that imported it.
_ADAPT_GLOBS = _PATH_GLOBS

# Chrome PWAs: chrome-<app-id>-Default.desktop. Not google-chrome.desktop.
_CHROME_PWA_DESKTOP = re.compile(
    r"^chrome-[a-z0-9]{16,}-Default\.desktop$", re.IGNORECASE
)
_LAUNCHER_LIST_KEY = re.compile(
    r"^(launchers|ordering|favorites|launchCounts|Items)\s*=\s*(.*)$",
    re.IGNORECASE,
)


def launcher_desktop_name(token: str) -> str:
    """applications:foo.desktop or file:///…/foo.desktop → foo.desktop."""
    name = token.strip()
    name = name.split("=", 1)[0].strip()
    name = name.removeprefix("applications:")
    if "://" in name:
        name = name.rsplit("/", 1)[-1]
    elif "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def is_ephemeral_launcher_id(token: str, *, drop_waydroid_pin: bool = False) -> bool:
    """Chrome PWAs and Waydroid Android apps are not restored as working apps."""
    name = launcher_desktop_name(token)
    if not name:
        return False
    low = name.lower()
    if _CHROME_PWA_DESKTOP.match(name):
        return True
    if low.startswith("waydroid.com."):
        return True
    if drop_waydroid_pin and low == "waydroid.desktop":
        return True
    return False


def is_ephemeral_desktop_file(path: Path) -> bool:
    """User .desktop stubs for Chrome PWAs and Waydroid Android apps."""
    name = path.name
    low = name.lower()
    if _CHROME_PWA_DESKTOP.match(name) or low.startswith("waydroid.com."):
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if "X-WayDroid-App" in text or "waydroid app launch" in text:
        return True
    if "--app-id=" in text and "chrome" in text.lower():
        return True
    return False


def filter_ephemeral_launchers(text: str, *, drop_waydroid_pin: bool = False) -> str:
    """Drop dead PWA / Waydroid Android entries from Plasma launcher lists."""
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        raw = line.rstrip("\r\n")
        nl = line[len(raw) :]
        m = _LAUNCHER_LIST_KEY.match(raw)
        if not m:
            lines.append(line)
            continue
        key, value = m.group(1), m.group(2)
        kept = [
            part
            for part in value.split(",")
            if part.strip()
            and not is_ephemeral_launcher_id(
                part, drop_waydroid_pin=drop_waydroid_pin
            )
        ]
        lines.append(f"{key}={','.join(kept)}{nl}")
    return "".join(lines)


def prune_ephemeral_launchers(
    home: Path, *, drop_waydroid_pin: bool | None = None
) -> dict[str, str]:
    """Remove Chrome PWA and Waydroid Android shortcuts from a home or overlay.

    Waydroid's own pin (Waydroid.desktop) is kept when the waydroid binary is
    on PATH — the RPM can be restored even though the Android data is not.
    """
    home = Path(home)
    if drop_waydroid_pin is None:
        drop_waydroid_pin = shutil.which("waydroid") is None
    removed = 0
    apps = home / ".local" / "share" / "applications"
    if apps.is_dir():
        for path in apps.glob("*.desktop"):
            if not path.is_file() or path.is_symlink():
                continue
            if is_ephemeral_desktop_file(path):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
    rewritten = 0
    for rel in (
        ".config/plasma-org.kde.plasma.desktop-appletsrc",
        ".config/plasmashellrc",
        ".config/kactivitymanagerd-statsrc",
    ):
        path = home / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out = filter_ephemeral_launchers(text, drop_waydroid_pin=drop_waydroid_pin)
        if out != text:
            path.write_text(out, encoding="utf-8")
            rewritten += 1
    return {"desktops": str(removed), "configs": str(rewritten)}


def rewrite_home_paths(text: str, old_home: str, new_home: str) -> str:
    """Replace one user's home with another's, including file:// URLs.

    `/home/cp` must not rewrite `/home/cpumares` — old home is only matched
    when the next character is a path separator or the end of the token.
    """
    old = old_home.rstrip("/")
    new = new_home.rstrip("/")
    if not old or old == new:
        return text
    pat = re.compile(re.escape(old) + r"(?=/|\"|'|\s|$|:)")
    return pat.sub(new, text)


def detect_embedded_homes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _HOME_RE.finditer(text):
        h = m.group(1)
        if h not in seen:
            seen.add(h)
            found.append(h)
    return found


def current_activity_id(kactivitymanagerdrc: str) -> str:
    """Default / current Plasma activity UUID, or empty."""
    current = ""
    in_activities = False
    first = ""
    for raw in kactivitymanagerdrc.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_activities = line.lower() == "[activities]"
            continue
        if not in_activities or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key.lower() == "current":
            current = val
        elif _UUID_RE.fullmatch(key) and not first:
            first = key
    return current or first


def activity_ids_in(text: str) -> set[str]:
    return {m.group(0).lower() for m in _UUID_RE.finditer(text)}


def remap_unmapped_activities(text: str, known_ids: set[str], target_id: str) -> str:
    """Point leftover activity UUIDs at the activity this session actually uses."""
    if not target_id or not _UUID_RE.fullmatch(target_id):
        return text
    known = {k.lower() for k in known_ids}
    known.add(target_id.lower())
    out = text
    for uid in sorted(activity_ids_in(text), key=len, reverse=True):
        if uid not in known:
            out = re.sub(re.escape(uid), target_id, out, flags=re.IGNORECASE)
    return out


def media_paths_from_config(text: str) -> list[str]:
    """Absolute local paths referenced as wallpaper / launcher images."""
    paths: list[str] = []
    for m in _MEDIA_KEY_RE.finditer(text):
        raw = m.group(2).strip()
        for part in raw.split(","):
            p = part.strip().removeprefix("file://")
            if p.startswith("/") and not p.startswith("/usr/"):
                paths.append(p)
    return paths


def _unique_dest(dest_dir: Path, src: Path) -> Path:
    candidate = dest_dir / src.name
    if not candidate.exists():
        return candidate
    stem, suf = src.stem, src.suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}-{n}{suf}"
        if not candidate.exists():
            return candidate
        n += 1


def detect_desktop_environment(env: dict[str, str] | None = None) -> str:
    """Map XDG_CURRENT_DESKTOP to a backup preset: kde, gnome, xfce, …, all."""
    src = env if env is not None else os.environ
    de = (src.get("XDG_CURRENT_DESKTOP") or src.get("DESKTOP_SESSION") or "").lower()
    if "kde" in de or "plasma" in de:
        return "kde"
    if "gnome" in de:
        return "gnome"
    if "xfce" in de:
        return "xfce"
    if "cinnamon" in de:
        return "cinnamon"
    if "mate" in de:
        return "mate"
    if "hypr" in de or "sway" in de or "niri" in de:
        return "hyprland"
    return "all"


def collect_plasma_media(home: Path, overlay: Path) -> int:
    """Copy wallpaper/icon files into a portable overlay dir and rewrite configs."""
    copied = 0
    dest_dir = overlay / PORTABLE_WALLPAPER_REL
    mapping: dict[str, str] = {}
    overlay_cfgs = [
        overlay / ".config/plasma-org.kde.plasma.desktop-appletsrc",
        overlay / ".config/kscreenlockerrc",
        overlay / ".config/plasmarc",
    ]
    # Live session files catch Pictures/ wallpapers even if the overlay copy
    # still has a stale path, or collect-media runs before appletsrc is synced.
    scan_cfgs = overlay_cfgs + [
        home / ".config/plasma-org.kde.plasma.desktop-appletsrc",
        home / ".config/kscreenlockerrc",
        home / ".config/plasmarc",
    ]
    seen: set[str] = set()
    for cfg in scan_cfgs:
        if not cfg.is_file():
            continue
        try:
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for src_s in media_paths_from_config(text):
            if src_s in seen:
                continue
            seen.add(src_s)
            src = Path(src_s)
            if not src.is_file():
                # Config in the overlay still has the backup machine's paths.
                rel = None
                try:
                    rel = src.relative_to(home)
                except ValueError:
                    rel = None
                if rel is not None:
                    alt = overlay / rel
                    if alt.is_file():
                        src = alt
                    else:
                        continue
                else:
                    continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(dest_dir, src)
            try:
                shutil.copy2(src, dest)
            except OSError:
                continue
            mapping[src_s] = str(home / PORTABLE_WALLPAPER_REL / dest.name)
            copied += 1

    if not mapping:
        return 0

    for cfg in overlay_cfgs:
        if not cfg.is_file():
            continue
        text = cfg.read_text(encoding="utf-8", errors="replace")
        for old, new in mapping.items():
            text = text.replace(f"file://{old}", f"file://{new}")
            text = text.replace(old, new)
        cfg.write_text(text, encoding="utf-8")
    return copied


def _known_activity_ids(actrc_text: str) -> set[str]:
    return {m.group(0).lower() for m in _UUID_RE.finditer(actrc_text)}


def adapt_restored_home(
    home: Path,
    old_home: str | None = None,
    keep_activity: str | None = None,
) -> dict[str, str]:
    """Rewrite restored Plasma configs for this $HOME and this activity.

    keep_activity: UUID Plasma is already using on this machine. Backup
    containments/favorites that still name the old activity are retargeted
    so widgets and wallpapers actually appear.

    Returns a small report dict for logs.
    """
    home = home.resolve()
    report = {"old_home": "", "activity": "", "files": "0"}

    sample_parts: list[str] = []
    for rel in _PATH_GLOBS:
        p = home / rel
        if p.is_file():
            try:
                sample_parts.append(p.read_bytes().decode("latin-1"))
            except OSError:
                pass
    sample = "\n".join(sample_parts)
    homes = detect_embedded_homes(sample)
    if old_home:
        src_home = old_home.rstrip("/")
    else:
        src_home = next((h for h in homes if h != str(home)), "")
    report["old_home"] = src_home

    actrc = home / ".config/kactivitymanagerdrc"
    act_text = ""
    if actrc.is_file():
        act_text = actrc.read_bytes().decode("latin-1")
    target = (keep_activity or "").strip() or current_activity_id(act_text)
    report["activity"] = target
    # When we must attach to a live session, every other UUID is "unmapped".
    known = {target.lower()} if keep_activity and target else _known_activity_ids(act_text)
    if target:
        known.add(target.lower())

    n = 0
    rels = list(_PATH_GLOBS)
    for extra in (
        home / ".local/share/applications",
        home / ".config/autostart",
        home / ".config/session",
    ):
        if extra.is_dir():
            if extra.name == "session":
                rels.extend(str(p.relative_to(home)) for p in extra.glob("dolphin*"))
            else:
                rels.extend(str(p.relative_to(home)) for p in extra.glob("*.desktop"))

    for rel in rels:
        path = home / rel
        if not path.is_file():
            continue
        if keep_activity and path.name == "kactivitymanagerdrc":
            continue
        try:
            text = path.read_bytes().decode("latin-1")
        except OSError:
            continue
        orig = text
        if src_home:
            text = rewrite_home_paths(text, src_home, str(home))
        if target and rel in _ACTIVITY_GLOBS:
            text = remap_unmapped_activities(text, known, target)
        if text != orig:
            path.write_bytes(text.encode("latin-1"))
            n += 1
    pruned = prune_ephemeral_launchers(home)
    report["files"] = str(n)
    report["pruned_desktops"] = pruned["desktops"]
    report["pruned_configs"] = pruned["configs"]
    return report


def _is_probably_text(path: Path) -> bool:
    suf = path.suffix.lower()
    if suf in _SKIP_SUFFIXES:
        return False
    name = path.name
    if name in {"MANIFEST.sha256", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size == 0 or size > _MAX_REWRITE_BYTES:
        return False
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return False
    return b"\x00" not in chunk


def iter_rewritable_files(root: Path):
    root = Path(root)
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            if _is_probably_text(path):
                yield path


def _rewrite_bytes(data: bytes, old: str, new: str) -> bytes:
    text = data.decode("latin-1")
    out = rewrite_home_paths(text, old, new)
    if out == text:
        return data
    return out.encode("latin-1")


def portable_backup_tree(root: Path, home: str) -> int:
    """Replace this machine's $HOME with HOME_TOKEN in a blueprint. Returns files changed."""
    home = str(Path(home))
    n = 0
    needle = home.encode("utf-8")
    for path in iter_rewritable_files(root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle not in data:
            continue
        new = _rewrite_bytes(data, home, HOME_TOKEN)
        if new != data:
            path.write_bytes(new)
            n += 1
    marker = Path(root) / "manifests" / "portable-home.txt"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"token={HOME_TOKEN}\n", encoding="utf-8")
    except OSError:
        pass
    return n


def _live_path_for(backup: Path, src: Path, live_home: Path) -> Path | None:
    pairs = (
        (backup / "config" / "home-overlay", live_home),
        (backup / "projects", live_home),
        (backup / "config" / "snap", live_home / "snap"),
    )
    extra = backup / "extra"
    try:
        rel = src.relative_to(extra)
        if rel.parts and rel.parts[0] == "_outside":
            rest = Path(*rel.parts[1:]) if len(rel.parts) > 1 else Path()
            return live_home / "UrStack-restored-outside" / rest
        return live_home / rel
    except ValueError:
        pass
    for src_root, dest_root in pairs:
        try:
            return dest_root / src.relative_to(src_root)
        except ValueError:
            continue
    return None


def materialize_backup_home(backup: Path, live_home: Path) -> int:
    """Expand HOME_TOKEN (and leftover /home/<old>) in files restored from backup."""
    backup = Path(backup)
    live_home = Path(live_home).resolve()
    n = 0
    token_b = HOME_TOKEN.encode("utf-8")
    for src_root in (
        backup / "config" / "home-overlay",
        backup / "extra",
        backup / "projects",
        backup / "config" / "snap",
    ):
        if not src_root.is_dir():
            continue
        for src in iter_rewritable_files(src_root):
            dest = _live_path_for(backup, src, live_home)
            if dest is None or not dest.is_file() or dest.is_symlink():
                continue
            try:
                data = dest.read_bytes()
            except OSError:
                continue
            orig = data
            if token_b in data:
                data = _rewrite_bytes(data, HOME_TOKEN, str(live_home))
            # Older blueprints still embed the source machine's home.
            sample = data.decode("latin-1")
            for old in detect_embedded_homes(sample):
                if old != str(live_home):
                    data = _rewrite_bytes(data, old, str(live_home))
            if data != orig:
                dest.write_bytes(data)
                n += 1
    return n


def _parse_kde_ini(text: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    cp.read_string(text)
    return cp


def _write_kde_ini(cp: configparser.ConfigParser) -> str:
    # Plasma is picky: no spaces around '='.
    lines: list[str] = []
    for section in cp.sections():
        lines.append(f"[{section}]")
        for key, value in cp.items(section):
            lines.append(f"{key}={value}")
        lines.append("")
    return "\n".join(lines)


def complete_colorscheme(kdeglobals: str, name: str = RESTORED_COLOR_SCHEME) -> str:
    """Turn restored kdeglobals colours into a Plasma .colors file.

    Panels read Colors:Complementary / Colors:Header. Backups that only store
    Window/View leave those empty, and Plasma then falls back to grey Breeze
    Dark (32,35,38) even when windows are near-black.
    """
    src = _parse_kde_ini(kdeglobals)
    out = configparser.ConfigParser(interpolation=None, strict=False)
    out.optionxform = str
    for section in _COLOR_SECTIONS:
        if not src.has_section(section):
            continue
        out.add_section(section)
        for key, value in src.items(section):
            out.set(section, key, value)
    if src.has_section("KDE"):
        out.add_section("KDE")
        for key in ("contrast", "frameContrast"):
            if src.has_option("KDE", key):
                out.set("KDE", key, src.get("KDE", key))
    window = "Colors:Window"
    if out.has_section(window) and out.has_option(window, "BackgroundNormal"):
        bg = out.get(window, "BackgroundNormal")
        for section in ("Colors:Complementary", "Colors:Header"):
            existed = out.has_section(section)
            had_bg = existed and out.has_option(section, "BackgroundNormal")
            had_alt = existed and out.has_option(section, "BackgroundAlternate")
            if not existed:
                out.add_section(section)
                for key, value in out.items(window):
                    if key.startswith("Foreground") or key.startswith("Decoration"):
                        out.set(section, key, value)
            if not had_bg:
                out.set(section, "BackgroundNormal", bg)
            if not had_alt:
                # Do not copy Window BackgroundAlternate: backups often leave
                # that as a light leftover (248,248,248) which greys the panel.
                out.set(section, "BackgroundAlternate", bg)
    if not out.has_section("General"):
        out.add_section("General")
    out.set("General", "ColorScheme", name)
    out.set("General", "Name", "UrStack restored")
    if src.has_section("General"):
        for key in ("AccentColor", "LastUsedCustomAccentColor", "shadeSortColumn"):
            if src.has_option("General", key):
                out.set("General", key, src.get("General", key))
    return _write_kde_ini(out)


def export_restored_colorscheme(home: Path, name: str = RESTORED_COLOR_SCHEME) -> Path | None:
    kdeglobals = home / ".config" / "kdeglobals"
    if not kdeglobals.is_file():
        return None
    text = kdeglobals.read_text(encoding="utf-8", errors="replace")
    if "[Colors:Window]" not in text:
        return None
    dest = home / ".local" / "share" / "color-schemes" / f"{name}.colors"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(complete_colorscheme(text, name), encoding="utf-8")
    return dest


def _cmd_collect(args: argparse.Namespace) -> int:
    n = collect_plasma_media(Path(args.home), Path(args.overlay))
    print(f"plasma-media: copied {n} wallpaper/icon file(s)")
    return 0


def _cmd_current_activity(args: argparse.Namespace) -> int:
    path = Path(args.home) / ".config/kactivitymanagerdrc"
    if not path.is_file():
        return 0
    print(current_activity_id(path.read_text(encoding="utf-8", errors="replace")))
    return 0


def _cmd_adapt(args: argparse.Namespace) -> int:
    report = adapt_restored_home(
        Path(args.home), old_home=args.old_home, keep_activity=args.keep_activity
    )
    print(
        "plasma-adapt: "
        f"old_home={report['old_home'] or '-'} "
        f"activity={report['activity'] or '-'} "
        f"rewrote={report['files']} "
        f"pruned_desktops={report.get('pruned_desktops', '0')} "
        f"pruned_configs={report.get('pruned_configs', '0')}"
    )
    return 0


def _cmd_portable(args: argparse.Namespace) -> int:
    n = portable_backup_tree(Path(args.root), args.home)
    print(f"portable-home: tokenized {n} file(s)")
    return 0


def _cmd_materialize(args: argparse.Namespace) -> int:
    n = materialize_backup_home(Path(args.backup), Path(args.home))
    print(f"portable-home: materialized {n} file(s) for this user")
    return 0


def _cmd_export_colorscheme(args: argparse.Namespace) -> int:
    dest = export_restored_colorscheme(Path(args.home))
    if dest is None:
        print("plasma-colors: no Colors:Window in kdeglobals", file=sys.stderr)
        return 1
    print(str(dest))
    return 0


def _cmd_prune_launchers(args: argparse.Namespace) -> int:
    drop = None
    if args.drop_waydroid_pin:
        drop = True
    elif args.keep_waydroid_pin:
        drop = False
    report = prune_ephemeral_launchers(Path(args.home), drop_waydroid_pin=drop)
    print(
        "prune-launchers: "
        f"desktops={report['desktops']} configs={report['configs']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect-media", help="Copy Plasma wallpapers into the overlay")
    c.add_argument("--home", default=os.environ.get("HOME", ""))
    c.add_argument("--overlay", required=True)
    c.set_defaults(func=_cmd_collect)
    a = sub.add_parser("adapt-home", help="Rewrite restored Plasma configs for this user")
    a.add_argument("--home", default=os.environ.get("HOME", ""))
    a.add_argument("--old-home", default=None)
    a.add_argument(
        "--keep-activity",
        default=None,
        help="Activity UUID already in use on this machine (remap backup IDs to it)",
    )
    a.set_defaults(func=_cmd_adapt)
    cur = sub.add_parser("current-activity", help="Print the live Plasma activity UUID")
    cur.add_argument("--home", default=os.environ.get("HOME", ""))
    cur.set_defaults(func=_cmd_current_activity)
    pb = sub.add_parser(
        "portable-backup",
        help="Replace $HOME with a token so the blueprint has no username",
    )
    pb.add_argument("--home", default=os.environ.get("HOME", ""))
    pb.add_argument("--root", required=True, help="Backup destination root")
    pb.set_defaults(func=_cmd_portable)
    mh = sub.add_parser(
        "materialize-home",
        help="Expand the home token (or leftover /home/<old>) onto this account",
    )
    mh.add_argument("--home", default=os.environ.get("HOME", ""))
    mh.add_argument("--backup", required=True, help="Backup root that was restored")
    mh.set_defaults(func=_cmd_materialize)
    cs = sub.add_parser(
        "export-colorscheme",
        help="Write restored kdeglobals colours as a Plasma color scheme",
    )
    cs.add_argument("--home", default=os.environ.get("HOME", ""))
    cs.set_defaults(func=_cmd_export_colorscheme)
    pr = sub.add_parser(
        "prune-launchers",
        help="Remove Chrome PWA and Waydroid Android shortcuts",
    )
    pr.add_argument("--home", default=os.environ.get("HOME", ""))
    pr.add_argument(
        "--drop-waydroid-pin",
        action="store_true",
        help="Also remove the Waydroid.desktop taskbar pin",
    )
    pr.add_argument(
        "--keep-waydroid-pin",
        action="store_true",
        help="Keep Waydroid.desktop even if waydroid is not on PATH",
    )
    pr.set_defaults(func=_cmd_prune_launchers)
    args = p.parse_args(argv)
    if not args.home:
        print("adapt_kde_restore: --home is required", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
