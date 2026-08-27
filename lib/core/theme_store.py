#!/usr/bin/env python3
"""Curated community themes from GitHub — not the GNOME/KDE store.

Discover and GNOME Software already list Pling / openDesktop. This catalog is
the Apps-page equivalent: hand-picked FOSS palettes and icon sets (Dracula,
Nord, Catppuccin, Sweet, Bibata) downloaded as real archives and installed
user-local by look.py. Archives are unpacked and applied with desktop
helpers; installer scripts inside a zip are never executed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_CTX = ssl.create_default_context()
_UA = "UrStack/1.0 (https://github.com/CDPumares/urstack; theme-store)"
_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "urstack" / "theme-store"
_APP_ROOT = Path(__file__).resolve().parents[2]
_CATALOG = _APP_ROOT / "data" / "catalog" / "themes.json"

MAX_BYTES = 400 * 1024 * 1024
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

ARCHIVE_SUFFIXES = (
    ".tar.xz",
    ".txz",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar",
    ".zip",
)


def _valid_github_repo(repo: str) -> bool:
    text = (repo or "").strip()
    return bool(_GITHUB_REPO_RE.fullmatch(text)) and ".." not in text


class ThemeStoreError(Exception):
    """Listing or download failed; safe to show in the UI."""


@dataclass(frozen=True)
class Category:
    id: str
    label: str
    desktops: tuple[str, ...] = ()  # empty = every desktop


CATEGORIES: tuple[Category, ...] = (
    Category("looks", "Desktop looks"),
    Category("gtk", "GTK themes"),
    Category("plasma", "Plasma", ("plasma",)),
    Category("icons", "Icons"),
    Category("cursors", "Cursors"),
)

_CATEGORY_BY_ID = {c.id: c for c in CATEGORIES}
_CATALOG_CACHE: dict[str, object] = {"mtime": None, "themes": []}


def cache_dir() -> Path:
    _CACHE.mkdir(parents=True, exist_ok=True)
    return _CACHE


def catalog_path() -> Path:
    return _CATALOG


def categories_for(desktop: str) -> list[str]:
    desk = (desktop or "unknown").strip().lower() or "unknown"
    out: list[str] = []
    for cat in CATEGORIES:
        if cat.desktops and desk not in cat.desktops:
            continue
        out.append(cat.id)
    return out


def default_kind(desktop: str) -> str:
    cats = categories_for(desktop)
    if "looks" in cats:
        return "looks"
    return cats[0] if cats else "gtk"


def category_label(kind: str) -> str:
    cat = _CATEGORY_BY_ID.get(kind)
    return cat.label if cat else kind.replace("-", " ").title()


def format_count(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        value = int(float(text))
    except ValueError:
        return text
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1000:
        return f"{value / 1000:.0f}k"
    return str(value)


def _progress(msg: str, pct: int | None = None) -> None:
    if msg:
        print(f"# {msg}", flush=True)
    if pct is not None:
        print(str(max(0, min(100, int(pct)))), flush=True)


def _archive_rank(name: str, tags: str = "") -> int:
    lower = (name or "").lower()
    for score, suffix in (
        (5, ".tar.xz"),
        (5, ".txz"),
        (4, ".tar.gz"),
        (4, ".tgz"),
        (3, ".tar.bz2"),
        (3, ".tbz2"),
        (2, ".zip"),
        (1, ".tar"),
    ):
        if lower.endswith(suffix):
            return score
    tags_l = (tags or "").lower()
    if "xz" in tags_l or "x-xz" in tags_l:
        return 5
    if "gzip" in tags_l or "x-gzip" in tags_l:
        return 4
    if "zip" in tags_l:
        return 2
    if "tar" in tags_l:
        return 1
    return 0


def safe_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._+-]+", "_", base).strip("._") or "theme"
    lower = base.lower()
    if not any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
        base += ".tar.xz"
    return base[:180]


def github_release_asset_url(repo: str, asset: str) -> str:
    if not _valid_github_repo(repo or ""):
        raise ThemeStoreError("Invalid GitHub repository")
    name = Path((asset or "").replace("\\", "/")).name
    if not name:
        raise ThemeStoreError("Missing release asset name")
    return (
        f"https://github.com/{repo}/releases/latest/download/"
        f"{urllib.parse.quote(name)}"
    )


def github_archive_url(repo: str, ref: str = "master", *, tagged: bool = False) -> str:
    if not _valid_github_repo(repo or ""):
        raise ThemeStoreError("Invalid GitHub repository")
    branch = (ref or "master").strip() or "master"
    if "/" in branch or "\\" in branch or branch.startswith("."):
        raise ThemeStoreError("Invalid git ref")
    kind = "tags" if tagged else "heads"
    return f"https://github.com/{repo}/archive/refs/{kind}/{urllib.parse.quote(branch)}.tar.gz"


def pick_github_asset(assets: list[dict], pattern: str = "") -> tuple[str, str] | None:
    """Pick a Linux theme archive from a GitHub release assets list."""
    skip = ("windows", "macos", "win32", "darwin", "_win.", "-win.", "_windows")
    ranked: list[tuple[int, str, str]] = []
    for raw in assets:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        url = str(raw.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        lower = name.lower()
        if any(token in lower for token in skip):
            continue
        rank = _archive_rank(name)
        if rank <= 0:
            continue
        if pattern:
            if any(ch in pattern for ch in "*?["):
                if not fnmatch.fnmatch(name, pattern) and not fnmatch.fnmatch(
                    name.lower(), pattern.lower()
                ):
                    continue
            elif name != pattern:
                continue
        ranked.append((rank, name, url))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return ranked[0][2], ranked[0][1]


def load_catalog(path: Path | None = None) -> list[dict[str, str]]:
    catalog = Path(path) if path is not None else _CATALOG
    try:
        mtime = catalog.stat().st_mtime
    except OSError:
        return []
    if _CATALOG_CACHE.get("path") == str(catalog) and _CATALOG_CACHE.get("mtime") == mtime:
        return list(_CATALOG_CACHE["themes"])  # type: ignore[arg-type]
    try:
        payload = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_list = payload.get("themes") if isinstance(payload, dict) else payload
    if not isinstance(raw_list, list):
        return []
    themes: list[dict[str, str]] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        repo = str(raw.get("github") or "").strip()
        if not cid or not name or not _valid_github_repo(repo):
            continue
        kinds = raw.get("kinds") or raw.get("kind") or "gtk"
        if isinstance(kinds, str):
            kinds_s = kinds
        else:
            kinds_s = ",".join(str(k).strip() for k in kinds if str(k).strip())
        desktops = raw.get("desktops") or []
        if isinstance(desktops, str):
            desks_s = desktops
        else:
            desks_s = ",".join(str(d).strip() for d in desktops if str(d).strip())
        themes.append(
            {
                "id": cid,
                "name": name,
                "summary": str(raw.get("summary") or "").strip(),
                "author": str(raw.get("author") or repo.split("/")[0]),
                "license": str(raw.get("license") or "").strip(),
                "kinds": kinds_s,
                "desktops": desks_s,
                "github": repo,
                "asset": str(raw.get("asset") or "").strip(),
                "ref": str(raw.get("ref") or "").strip(),
                "ref_kind": str(raw.get("ref_kind") or "").strip(),
                "homepage": str(raw.get("homepage") or f"https://github.com/{repo}").strip(),
                "preview": str(raw.get("preview") or "").strip(),
                "host": "catalog",
                "detailpage": str(raw.get("homepage") or f"https://github.com/{repo}").strip(),
            }
        )
    _CATALOG_CACHE["path"] = str(catalog)
    _CATALOG_CACHE["mtime"] = mtime
    _CATALOG_CACHE["themes"] = themes
    return list(themes)


def catalog_entry(content_id: str, path: Path | None = None) -> dict[str, str] | None:
    want = (content_id or "").strip()
    if not want:
        return None
    for row in load_catalog(path):
        if row["id"] == want:
            return row
    return None


def entry_kinds(row: dict[str, str]) -> list[str]:
    return [part for part in (row.get("kinds") or "").split(",") if part]


def entry_desktops(row: dict[str, str]) -> list[str]:
    return [part for part in (row.get("desktops") or "").split(",") if part]


def row_visible(row: dict[str, str], kind: str, desktop: str, search: str = "") -> bool:
    kinds = entry_kinds(row)
    if kind and kind not in kinds:
        return False
    desks = entry_desktops(row)
    desk = (desktop or "unknown").strip().lower()
    if desks and desk not in desks and desk != "unknown":
        return False
    q = (search or "").strip().lower()
    if not q:
        return True
    blob = " ".join(
        [
            row.get("id") or "",
            row.get("name") or "",
            row.get("summary") or "",
            row.get("author") or "",
            row.get("github") or "",
            row.get("license") or "",
        ]
    ).lower()
    return q in blob


def list_themes(
    kind: str,
    desktop: str,
    *,
    search: str = "",
    catalog: Path | None = None,
) -> tuple[list[dict[str, str]], str]:
    rows = [
        dict(row)
        for row in load_catalog(catalog)
        if row_visible(row, kind, desktop, search)
    ]
    for row in rows:
        row["kind"] = kind
        row["typename"] = category_label(kind)
    return rows, "the UrStack catalog"


def resolve_download(entry: dict[str, str]) -> tuple[str, str]:
    repo = (entry.get("github") or "").strip()
    asset = (entry.get("asset") or "").strip()
    if asset:
        return github_release_asset_url(repo, asset), asset
    ref = (entry.get("ref") or "master").strip() or "master"
    tagged = (entry.get("ref_kind") or "").strip().lower() == "tag"
    url = github_archive_url(repo, ref, tagged=tagged)
    return url, f"{repo.split('/')[-1]}-{ref}.tar.gz"


def fetch_json(url: str, timeout: int = 25) -> dict | list | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"user-agent": _UA, "accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, (dict, list)) else None


def fetch_bytes(url: str, timeout: int = 25) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"user-agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return data or None


def cached_preview(url: str) -> Path | None:
    src = (url or "").strip()
    if not src or not src.startswith(("http://", "https://")):
        return None
    digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:24]
    lower = src.lower()
    ext = ".jpg"
    if ".png" in lower:
        ext = ".png"
    elif ".webp" in lower:
        ext = ".webp"
    path = cache_dir() / "previews" / f"{digest}{ext}"
    if path.is_file() and path.stat().st_size > 40:
        return path
    data = fetch_bytes(src, timeout=20)
    if not data or len(data) < 40:
        return None
    head = data[:48].lstrip().lower()
    if head.startswith(b"<") or head.startswith(b"<!doctype"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def download_to(url: str, dest: Path, *, max_bytes: int = MAX_BYTES) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"user-agent": _UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=90, context=_CTX) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise ThemeStoreError("This listing linked to a web page, not a theme archive")
            total = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            got = 0
            with tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > max_bytes:
                        raise ThemeStoreError("Download is larger than 400 MB")
                    fh.write(chunk)
                    if total > 0:
                        _progress("", 12 + int(38 * got / total))
    except ThemeStoreError:
        tmp.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        raise ThemeStoreError(f"Download failed ({exc})") from exc
    tmp.replace(dest)


def install_from_store(host: str, content_id: str, *, home: Path | None = None) -> None:
    """Download a catalogued GitHub theme archive and install it user-local."""
    import look as look_engine

    if (host or "catalog").strip() not in {"catalog", "github"}:
        raise ThemeStoreError(
            "Browse themes is the GitHub catalog, not the GNOME/KDE store"
        )
    _progress("Looking up the pack…", 4)
    entry = catalog_entry(content_id)
    if entry is None:
        raise ThemeStoreError("Unknown theme in the catalog")
    url, name = resolve_download(entry)
    filename = safe_filename(name)
    _progress(f"Downloading {entry['name']} from GitHub…", 12)
    staging = Path(tempfile.mkdtemp(prefix="urstack-theme-dl-"))
    dest = staging / filename
    try:
        download_to(url, dest)
        _progress("Installing…", 52)
        look_engine.install_archive(
            dest,
            home=home if home is not None else Path.home(),
            apply=True,
            progress_base=52,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
