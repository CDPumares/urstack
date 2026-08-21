# UrStack app catalog — install popular apps by category
# shellcheck shell=bash

catalog_file() {
  echo "${URSTACK_ROOT:-${STACKUP_ROOT:-$FEDORA_UPDATES_ROOT}}/data/catalog/apps.json"
}

catalog_dir() {
  echo "${URSTACK_ROOT:-${STACKUP_ROOT:-$FEDORA_UPDATES_ROOT}}/data/catalog"
}

catalog_appimage_dir() {
  local d="$HOME/Applications"
  mkdir -p "$d"
  echo "$d"
}

# Ensure Flathub remote exists (user)
catalog_ensure_flathub() {
  command -v flatpak &>/dev/null || return 1
  if ! flatpak remotes --columns=name 2>/dev/null | grep -qx flathub; then
    flatpak remote-add --if-not-exists --user flathub https://dl.flathub.org/repo/flathub.flatpakrepo \
      || flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || true
  fi
}

# Run privileged work through the hardened helper rather than a root shell.
_catalog_priv() {
  local jobs ec
  jobs=$(mktemp) || return 1
  chmod 600 "$jobs"
  printf '%s\n' "$*" > "$jobs"
  pkexec_priv "$jobs" 2>&1
  ec=$?
  rm -f "$jobs"
  return "$ec"
}

# `dnf install <file>` does not check signatures: localpkg_gpgcheck defaults to
# false, so an unsigned or swapped RPM would run its %post scriptlet as root.
# The helper pins localpkg_gpgcheck=1 and re-validates the path.
_catalog_install_rpm() {
  _catalog_priv install_local_rpm "$1"
}

# Package names reach dnf/snap as root, where a leading dash is a flag.
_catalog_valid_pkg_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*$ ]]
}

# Anything fetched over plain http can be rewritten in transit, and these
# downloads are either executed or installed as root.
_catalog_require_https() {
  case "$1" in
    https://*) return 0 ;;
    *) echo "# Refusing non-HTTPS URL: $1"; return 1 ;;
  esac
}

catalog_ensure_cursor_repo() {
  [[ -f /etc/yum.repos.d/cursor.repo ]] && return 0
  _catalog_priv ensure_cursor_repo
}

# Install one catalog app.
# Args: method package name [url]
catalog_install_app() {
  local method="$1" package="$2" url="${4:-}"
  # Separate statement: $package is not yet visible inside the `local` that sets it.
  local name="${3:-$package}"
  echo "# Installing $name ($method)..."
  case "$method" in
    flatpak)
      catalog_ensure_flathub
      if flatpak install -y --user flathub "$package" 2>&1; then
        return 0
      fi
      flatpak install -y --system flathub "$package" 2>&1
      ;;
    snap)
      if ! _catalog_valid_pkg_name "$package"; then
        echo "# Refusing suspicious package name: $package"; return 1
      fi
      _catalog_priv snap_install "$package"
      ;;
    dnf)
      if ! _catalog_valid_pkg_name "$package"; then
        echo "# Refusing suspicious package name: $package"; return 1
      fi
      _catalog_priv dnf_install_pkg "$package"
      ;;
    cursor_rpm)
      catalog_ensure_cursor_repo || true
      if _catalog_priv dnf_install_pkg cursor; then
        return 0
      fi
      # Fallback: update API → direct RPM
      local installed latest rpm_url body http_code
      installed=$(rpm -q --qf '%{VERSION}' cursor 2>/dev/null || echo "0.0.0")
      body=$(mktemp)
      http_code=$(curl -sS -o "$body" -w '%{http_code}' \
        "https://api2.cursor.sh/updates/api/update/linux-x64/cursor/${installed}/stable" 2>/dev/null) || true
      if [[ "$http_code" == "200" ]]; then
        rpm_url=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); u=d.get("url","");
import re; h=re.search(r"/production/([^/]+)/", u); print(("https://downloads.cursor.com/production/%s/linux/x64/rpm/x86_64/cursor-%s.el8.x86_64.rpm"%(h.group(1), d.get("version",""))) if h else "")' "$body" 2>/dev/null) || true
      fi
      rm -f "$body"
      if [[ -n "$rpm_url" ]]; then
        local rpm_tmp
        rpm_tmp=$(mktemp --suffix=.rpm)
        echo "# Downloading Cursor RPM..."
        curl -fL --progress-bar -o "$rpm_tmp" "$rpm_url" \
          && _catalog_install_rpm "$rpm_tmp"
        local ec=$?
        rm -f "$rpm_tmp"
        return $ec
      fi
      return 1
      ;;
    rpm_url)
      [[ -n "$url" ]] || { echo "# Missing RPM URL"; return 1; }
      _catalog_require_https "$url" || return 1
      local rpm_tmp
      rpm_tmp=$(mktemp --suffix=.rpm)
      echo "# Downloading $url"
      curl -fL --progress-bar -o "$rpm_tmp" "$url" \
        && _catalog_install_rpm "$rpm_tmp"
      local ec=$?
      rm -f "$rpm_tmp"
      return $ec
      ;;
    appimage)
      [[ -n "$url" ]] || { echo "# Missing AppImage URL"; return 1; }
      local dest dir
      dir=$(catalog_appimage_dir)
      dest="$dir/${package}.AppImage"
      _catalog_require_https "$url" || return 1
      echo "# Downloading AppImage → $dest"
      curl -fL --proto '=https' --tlsv1.2 --progress-bar -o "$dest" "$url" || return 1
      chmod +x "$dest"
      # Desktop entry
      local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
      mkdir -p "$apps"
      cat > "$apps/${package}.desktop" <<EOF
[Desktop Entry]
Name=$name
Exec=$dest
Icon=application-x-executable
Type=Application
Categories=Utility;
Terminal=false
EOF
      echo "# Installed AppImage + menu entry"
      return 0
      ;;
    script)
      [[ -n "$url" ]] || { echo "# Missing install script URL"; return 1; }
      _catalog_require_https "$url" || return 1
      echo "# Running vendor install script from $url"
      echo "# (curated URL only — review UrStack catalog if unsure)"
      # Download first, then run: piping straight into bash lets a truncated
      # transfer execute half a script, and leaves nothing to inspect on failure.
      local script_tmp script_ec
      script_tmp=$(mktemp) || return 1
      if ! curl -fsSL --proto '=https' --tlsv1.2 -o "$script_tmp" "$url"; then
        echo "# Download failed: $url"; rm -f "$script_tmp"; return 1
      fi
      if [[ ! -s "$script_tmp" ]]; then
        echo "# Refusing to run an empty install script"; rm -f "$script_tmp"; return 1
      fi
      bash "$script_tmp"
      script_ec=$?
      rm -f "$script_tmp"
      return $script_ec
      ;;
    toolbox_tarball)
      # JetBrains Toolbox style: download tarball, extract to ~/.local/share, run installer
      [[ -n "$url" ]] || {
        # Resolve latest Toolbox Linux tarball
        url=$(curl -fsSL 'https://data.services.jetbrains.com/products/releases?code=TBA&latest=true&type=release' \
          | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["TBA"][0]["downloads"]["linux"]["link"])' 2>/dev/null) || true
      }
      [[ -n "$url" ]] || { echo "# Could not resolve Toolbox download URL"; return 1; }
      local tmp dir
      tmp=$(mktemp -d)
      echo "# Downloading JetBrains Toolbox..."
      curl -fL --progress-bar -o "$tmp/toolbox.tar.gz" "$url" || { rm -rf "$tmp"; return 1; }
      tar -xzf "$tmp/toolbox.tar.gz" -C "$tmp"
      local bin
      bin=$(find "$tmp" -type f -name jetbrains-toolbox | head -1)
      [[ -n "$bin" ]] || { echo "# toolbox binary missing in archive"; rm -rf "$tmp"; return 1; }
      mkdir -p "$HOME/.local/share/JetBrains/Toolbox/bin" "$HOME/.local/bin"
      install -m 755 "$bin" "$HOME/.local/share/JetBrains/Toolbox/bin/jetbrains-toolbox"
      ln -sfn "$HOME/.local/share/JetBrains/Toolbox/bin/jetbrains-toolbox" "$HOME/.local/bin/jetbrains-toolbox"
      rm -rf "$tmp"
      echo "# JetBrains Toolbox installed — launching once to finish setup..."
      nohup "$HOME/.local/bin/jetbrains-toolbox" </dev/null &>/dev/null &
      return 0
      ;;
    browser)
      [[ -n "$url" ]] || { echo "# Missing download page URL"; return 1; }
      echo "# Opening download page (not in Fedora/Flathub stores): $url"
      xdg-open "$url" &>/dev/null || true
      return 0
      ;;
    *)
      echo "# Unknown install method: $method"
      return 1
      ;;
  esac
}

# Build a status file for the UI:
# id|name|summary|category|category_id|method|package|installed|url|badge|icon
# Merges data/catalog/*.json (apps.json first), deduping winutil imports by package/name.
catalog_status_file() {
  local out="$1"
  local dir
  dir=$(catalog_dir)
  [[ -d "$dir" ]] || { : > "$out"; return 1; }

  python3 - "$dir" "$out" <<'PY'
import json, os, sys, subprocess
from pathlib import Path

catalog_dir, dest = Path(sys.argv[1]), sys.argv[2]
home = Path.home()
lines = []

# Optional prebuilt logo map (app id → Flathub / Simple Icons / favicon URL)
_icon_map: dict[str, str] = {}
_map_path = catalog_dir / "icon-map.json"
if _map_path.is_file():
    try:
        _raw = json.load(open(_map_path, encoding="utf-8"))
        for _aid, _meta in (_raw.get("icons") or {}).items():
            if isinstance(_meta, str) and _meta.startswith("http"):
                _icon_map[_aid] = _meta
            elif isinstance(_meta, dict):
                _u = (_meta.get("icon") or "").strip()
                if _u.startswith("http"):
                    _icon_map[_aid] = _u
                elif _meta.get("icon_id"):
                    _icon_map[_aid] = (
                        f"https://dl.flathub.org/media/icons/128x128/{_meta['icon_id']}.png"
                    )
    except (OSError, json.JSONDecodeError):
        pass

def icon_for(app: dict) -> str:
    aid = app.get("id") or ""
    if aid in _icon_map:
        return _icon_map[aid]
    explicit = (app.get("icon") or "").strip()
    if explicit.startswith("http"):
        return explicit
    if app.get("icon_id"):
        return f"https://dl.flathub.org/media/icons/128x128/{app['icon_id']}.png"
    if app.get("method") == "flatpak" and app.get("package"):
        return f"https://dl.flathub.org/media/icons/128x128/{app['package']}.png"
    return ""


# Desktop launches often have a thin PATH — include common user tool roots.
_extra_bins: list[Path] = [
    home / ".local" / "bin",
    home / ".cargo" / "bin",
    home / "bin",
]
_nvm_root = home / ".nvm" / "versions" / "node"
if _nvm_root.is_dir():
    for ver_dir in sorted(_nvm_root.iterdir(), reverse=True):
        b = ver_dir / "bin"
        if b.is_dir():
            _extra_bins.append(b)
            break
_path_prefix = os.pathsep.join(str(p) for p in _extra_bins if p.is_dir())
if _path_prefix:
    os.environ["PATH"] = _path_prefix + os.pathsep + os.environ.get("PATH", "")

def _run_lines(cmd: list[str], timeout: int = 30) -> list[str]:
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

_flatpak_ids: set[str] = set()
for _cmd in (
    ["flatpak", "list", "--app", "--columns=application"],
    ["flatpak", "list", "--app", "--user", "--columns=application"],
    ["flatpak", "list", "--app", "--system", "--columns=application"],
):
    for _ln in _run_lines(_cmd):
        if _ln.lower() == "application":
            continue
        _flatpak_ids.add(_ln.split("/")[0])

_rpm_names: set[str] = set(_run_lines(["rpm", "-qa", "--qf", "%{NAME}\n"]))

_snap_names: set[str] = set()
for _i, _ln in enumerate(_run_lines(["snap", "list"])):
    if _i == 0:
        continue
    _snap_names.add(_ln.split()[0])

def which(cmd: str) -> bool:
    from shutil import which as w
    if not cmd:
        return False
    if w(cmd):
        return True
    for p in _extra_bins:
        candidate = p / cmd
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return True
    return False

# Desktop entries are the best cross-source signal: a distro build of a GUI app
# normally ships the same reverse-DNS id its Flathub build uses, so the okular
# RPM installs org.kde.okular.desktop.
_desktop_ids: set[str] = set()
_desktop_roots = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    home / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    home / ".local/share/flatpak/exports/share/applications",
    # Snap exports here and lists it in XDG_DATA_DIRS.
    Path("/var/lib/snapd/desktop/applications"),
]
for _d in os.environ.get("XDG_DATA_DIRS", "").split(os.pathsep):
    if _d.strip():
        _desktop_roots.append(Path(_d.strip()) / "applications")
for _app_root in _desktop_roots:
    try:
        for _entry in _app_root.glob("*.desktop"):
            _desktop_ids.add(_entry.stem)
            _desktop_ids.add(_entry.stem.lower())
            # Snap uses <snap>_<app>.desktop; register both halves.
            if "_" in _entry.stem:
                for _half in _entry.stem.lower().split("_"):
                    if _half:
                        _desktop_ids.add(_half)
    except OSError:
        pass


def _tail(pkg: str) -> str:
    """Last component of a reverse-DNS id: org.kde.okular -> okular."""
    return (pkg.rsplit(".", 1)[-1] if "." in pkg else pkg).lower()


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


_flatpak_tails: set[str] = {_tail(i) for i in _flatpak_ids}
_desktop_tails: set[str] = {_tail(d) for d in _desktop_ids}
_norm_flatpak_tails: set[str] = {_norm(t) for t in _flatpak_tails}
_norm_snap_names: set[str] = {_norm(s) for s in _snap_names}

# Trailing words that identify no app on their own: org.telegram.desktop and
# com.bitwarden.desktop would otherwise both match on "desktop".
_GENERIC_TAILS = frozenset(
    {"desktop", "client", "app", "gui", "www", "net", "com", "org", "io", "www2"}
)
# Vendor components shared by unrelated apps, so org.gnome.X must never
# degrade into a match on "gnome".
_GENERIC_VENDORS = frozenset(
    {
        "gnome", "kde", "github", "gitlab", "google", "microsoft", "apple",
        "freedesktop", "com", "org", "net", "io", "app", "get", "sh", "world",
    }
)


def _has_desktop_entry(name: str) -> bool:
    """A desktop entry for `name`, allowing for a split package family."""
    if name in _desktop_ids or name in _desktop_tails:
        return True
    return any(d.startswith(name + "-") for d in _desktop_ids)


def _native_present(name: str) -> bool:
    """Is a native package, snap, binary or desktop entry for `name` present?"""
    if not name:
        return False
    if name in _snap_names or name in _flatpak_tails:
        return True
    normed = _norm(name)
    if len(normed) >= 5 and (normed in _norm_flatpak_tails or normed in _norm_snap_names):
        return True
    # A desktop entry is required before trusting a bare name, so an unrelated
    # CLI tool that merely shares it (`boxes`, `net`) is not counted as the app.
    if not _has_desktop_entry(name):
        return False
    if name in _rpm_names or which(name):
        return True
    # Fedora splits some apps across a family with no bare package of that name
    # (there is no `libreoffice` RPM, only libreoffice-core and friends).
    return any(r.startswith(name + "-") for r in _rpm_names)


def _cross_source_installed(package: str) -> bool:
    """
    True when an app is present through a mechanism other than the one the
    catalog happens to prefer — Okular is catalogued as the org.kde.okular
    Flatpak but on KDE Fedora it is usually the `okular` RPM.
    """
    if not package:
        return False
    if package in _flatpak_ids or package in _rpm_names or package in _snap_names:
        return True
    if package in _desktop_ids or package.lower() in _desktop_ids:
        return True

    candidates: set[str] = set()
    parts = package.split(".")
    if len(parts) >= 3:
        # Reverse-DNS id. Two dots minimum keeps `battle.net` from yielding "net".
        tail, vendor = parts[-1].lower(), parts[-2].lower()
        if len(tail) >= 4 and tail not in _GENERIC_TAILS:
            candidates.add(tail)
        if vendor not in _GENERIC_VENDORS and len(vendor) >= 4:
            candidates.add(f"{vendor}-{tail}")
            candidates.add(f"{vendor}{tail}")
            # com.spotify.Client is just "spotify" natively.
            if tail in _GENERIC_TAILS:
                candidates.add(vendor)
    elif "." not in package:
        candidates.add(package.lower())

    return any(_native_present(c) for c in candidates)


def installed(app: dict) -> str:
    method = app.get("method", "")
    package = app.get("package", "")
    detect = app.get("detect") or package
    # Commands that often differ from the RPM/Flatpak id
    aliases = {
        "nodejs": ("node", "nodejs"),
        "nodejslts": ("node", "nodejs"),
        "google-chrome-stable": ("google-chrome", "google-chrome-stable"),
        "code": ("code", "code-oss"),
    }
    try:
        if method == "flatpak":
            if package in _flatpak_ids:
                return "1"
            return "1" if _cross_source_installed(package) else "0"
        if method == "snap":
            if package in _snap_names:
                return "1"
            return "1" if _cross_source_installed(package) else "0"
        if method in {"dnf", "cursor_rpm", "rpm_url"}:
            if package in _rpm_names:
                return "1"
            if which(detect):
                return "1"
            for alt in aliases.get(package, ()) + aliases.get(detect, ()):
                if which(alt) or alt in _rpm_names:
                    return "1"
            return "1" if _cross_source_installed(package) else "0"
        if method == "appimage":
            p = home / "Applications" / f"{package}.AppImage"
            return "1" if p.is_file() else "0"
        if method == "toolbox_tarball":
            return "1" if which("jetbrains-toolbox") or (home / ".local/share/JetBrains/Toolbox/bin/jetbrains-toolbox").is_file() else "0"
        if method in {"script", "browser"}:
            if which(detect):
                return "1"
            for alt in aliases.get(package, ()) + aliases.get(detect, ()):
                if which(alt):
                    return "1"
            return "1" if _cross_source_installed(package) else "0"
        if which(detect):
            return "1"
        for alt in aliases.get(package, ()) + aliases.get(detect, ()):
            if which(alt):
                return "1"
        return "1" if _cross_source_installed(package) else "0"
    except FileNotFoundError:
        return "0"

def clean(s):
    return (s or "").replace("|", "/").replace("\n", " ")

def norm_name(s: str) -> str:
    return "".join(ch.lower() for ch in (s or "") if ch.isalnum())

# Prefer curated apps.json, then winutil.json, then any other *.json
files = []
primary = catalog_dir / "apps.json"
winutil = catalog_dir / "winutil.json"
if primary.is_file():
    files.append(primary)
if winutil.is_file():
    files.append(winutil)
for p in sorted(catalog_dir.glob("*.json")):
    if p.name == "icon-map.json" or p in files:
        continue
    files.append(p)

# Map winutil-* / "(WinUtil)" labels onto the curated category names.
CANONICAL_CATS = {
    "browsers": "Browsers",
    "communication": "Communication",
    "media": "Media",
    "productivity": "Productivity",
    "developer": "Developer",
    "cli": "CLIs & tools",
    "direct": "Outside app stores",
    "graphics": "Graphics and design",
    "utilities": "Utilities",
    "gaming": "Gaming",
    "microsoft": "Microsoft tools",
    "pro-tools": "Pro tools",
    "selfhosted": "Self-hosted",
}

def normalize_category(cid: str, cname: str) -> tuple[str, str]:
    cid = (cid or "").strip()
    cname = (cname or "").strip()
    if cid.startswith("winutil-"):
        cid = cid[len("winutil-"):]
    # Drop legacy WinUtil suffixes from older imports
    for suffix in (" (WinUtil)", " (winutil)"):
        if cname.endswith(suffix):
            cname = cname[: -len(suffix)].rstrip()
    if cid in CANONICAL_CATS:
        cname = CANONICAL_CATS[cid]
    elif not cname:
        cname = cid.replace("-", " ").title() or "Other"
    return cid, cname

seen_ids: set[str] = set()
seen_packages: set[str] = set()
seen_names: set[str] = set()

for path in files:
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    is_winutil = path.name == "winutil.json" or data.get("source", "").endswith("winutil")
    for cat in data.get("categories", []):
        cat_id, cat_name = normalize_category(cat.get("id", ""), cat.get("name", ""))
        for app in cat.get("apps", []):
            # Skip Windows-only winutil entries — UrStack targets Fedora/Linux
            store = app.get("store", "")
            if store == "windows":
                continue
            aid = app.get("id") or ""
            package = (app.get("package") or "").strip()
            nname = norm_name(app.get("name") or "")
            if aid in seen_ids:
                continue
            # Deduplicate winutil imports against curated UrStack entries
            if is_winutil and (
                (package and package in seen_packages)
                or (nname and nname in seen_names)
            ):
                continue
            seen_ids.add(aid)
            if package:
                seen_packages.add(package)
            if nname:
                seen_names.add(nname)

            method = app.get("method", "")
            url = app.get("url", "")
            store = app.get("store", "flathub" if method == "flatpak" else method)
            if store == "windows":
                badge = "windows"
            elif store in {"external", "vendor", "direct"} or method in {
                "appimage", "rpm_url", "cursor_rpm", "script", "toolbox_tarball", "browser"
            }:
                badge = "vendor"
            else:
                badge = method
            lines.append("|".join([
                clean(aid),
                clean(app.get("name")),
                clean(app.get("summary")),
                clean(cat_name),
                clean(cat_id),
                clean(method),
                clean(package),
                installed(app),
                clean(url),
                clean(badge),
                clean(icon_for(app)),
                clean(app.get("repo_hint") or ""),
            ]))

open(dest, "w", encoding="utf-8").write("\n".join(lines) + ("\n" if lines else ""))
PY
}
