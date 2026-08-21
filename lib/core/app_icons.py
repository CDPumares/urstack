#!/usr/bin/env python3
"""Resolve app logos for the Apps catalog.

Prefers PNGs shipped in data/catalog/icons/, then the per-user download cache.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path
from collections.abc import Callable

_CTX = ssl.create_default_context()
_UA = "UrStack/1.0 (app-icons)"
_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "stackup" / "app-icons"
_MAP: dict[str, str] | None = None
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_APP_ROOT = Path(__file__).resolve().parents[2]


def icon_cache_dir() -> Path:
    _CACHE.mkdir(parents=True, exist_ok=True)
    return _CACHE


def safe_icon_stem(app_id: str) -> str:
    """Filesystem-safe filename stem for a catalog app id."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (app_id or "").strip())
    return stem.strip("._") or "app"


def bundled_icons_dir(catalog_dir: Path | None = None) -> Path:
    """Directory of vendored catalog logos (app_id.png)."""
    if catalog_dir is not None:
        return catalog_dir / "icons"
    env_root = os.environ.get("STACKUP_ROOT") or os.environ.get("FEDORA_UPDATES_ROOT")
    if env_root:
        bundled = Path(env_root) / "data" / "catalog" / "icons"
        if bundled.is_dir():
            return bundled
    return _APP_ROOT / "data" / "catalog" / "icons"


def bundled_icon_path(app_id: str, catalog_dir: Path | None = None) -> Path | None:
    """Return the shipped logo for this catalog id, if present."""
    if not (app_id or "").strip():
        return None
    stem = safe_icon_stem(app_id)
    base = bundled_icons_dir(catalog_dir)
    for ext in (".png", ".svg", ".ico"):
        path = base / f"{stem}{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def load_icon_map(catalog_dir: Path | None = None) -> dict[str, str]:
    """Return app_id → icon URL from data/catalog/icon-map.json."""
    global _MAP
    if _MAP is not None:
        return _MAP
    candidates: list[Path] = []
    if catalog_dir is not None:
        candidates.append(catalog_dir / "icon-map.json")
    root = Path(__file__).resolve().parents[2]
    candidates.append(root / "data" / "catalog" / "icon-map.json")
    env_root = os.environ.get("STACKUP_ROOT") or os.environ.get("FEDORA_UPDATES_ROOT")
    if env_root:
        candidates.append(Path(env_root) / "data" / "catalog" / "icon-map.json")

    mapping: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        icons = data.get("icons") or {}
        for app_id, meta in icons.items():
            if isinstance(meta, str) and meta.startswith("http"):
                mapping[app_id] = meta
            elif isinstance(meta, dict):
                url = (meta.get("icon") or "").strip()
                if url.startswith("http"):
                    mapping[app_id] = url
                elif meta.get("icon_id"):
                    mapping[app_id] = (
                        f"https://dl.flathub.org/media/icons/128x128/{meta['icon_id']}.png"
                    )
        break
    _MAP = mapping
    return mapping


def flathub_icon_url(app_id: str) -> str:
    return f"https://dl.flathub.org/media/icons/128x128/{app_id}.png"


def icon_url_for_row(row: dict[str, str], icon_map: dict[str, str] | None = None) -> str:
    """Pick the best icon URL for a catalog status row."""
    explicit = (row.get("icon") or "").strip()
    if explicit.startswith("http"):
        return explicit
    amap = icon_map if icon_map is not None else load_icon_map()
    if row.get("id") in amap:
        return amap[row["id"]]
    method = (row.get("method") or "").strip()
    package = (row.get("package") or "").strip()
    if method == "flatpak" and package:
        return flathub_icon_url(package)
    if package.count(".") >= 2 and not package.startswith("http"):
        return flathub_icon_url(package)
    return ""


def icon_path_for_row(row: dict[str, str], icon_map: dict[str, str] | None = None) -> Path | None:
    """Local logo path: bundled first, then the user download cache."""
    bundled = bundled_icon_path(row.get("id") or "")
    if bundled is not None:
        return bundled
    url = icon_url_for_row(row, icon_map)
    return cached_icon_path(url) if url else None


def _cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    # Prefer png extension for cached rasterized SVGs
    ext = ".svg" if url.lower().endswith(".svg") else ".bin"
    if "favicons" in url or url.lower().endswith(".png"):
        ext = ".png"
    elif url.lower().endswith(".ico"):
        ext = ".ico"
    return icon_cache_dir() / f"{digest}{ext}"


def cached_icon_path(url: str) -> Path | None:
    if not url:
        return None
    path = _cache_path_for_url(url)
    if path.is_file() and path.stat().st_size > 0:
        return path
    # Also accept rasterized companion
    png = path.with_suffix(".png")
    if png.is_file() and png.stat().st_size > 0:
        return png
    return None


def _fetch_url(url: str) -> tuple[bytes, str] | None:
    try:
        req = urllib.request.Request(url, headers={"user-agent": _UA})
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
            data = resp.read()
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    if not data:
        return None
    return data, ctype


def _write_png(pix, png_path: Path) -> bool:
    try:
        w, h = pix.get_width(), pix.get_height()
        if max(w, h) > 128:
            scale = 128 / float(max(w, h))
            from gi.repository import GdkPixbuf

            pix = pix.scale_simple(int(w * scale), int(h * scale), GdkPixbuf.InterpType.BILINEAR)
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pix.savev(str(png_path), "png", [], [])
        return png_path.is_file() and png_path.stat().st_size > 0
    except Exception:
        return False


def _rasterize_to_png(data: bytes, ctype: str, url: str, png_path: Path) -> bool:
    """Decode image bytes and write a PNG. Returns True on success."""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except Exception:
        return False

    is_svg = (
        ctype == "image/svg+xml"
        or url.lower().endswith(".svg")
        or data[:200].lstrip().startswith(b"<svg")
    )
    try:
        if is_svg:
            loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
            loader.set_size(128, 128)
        else:
            loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pix = loader.get_pixbuf()
        if pix is None:
            return False
        return _write_png(pix, png_path)
    except Exception:
        return False


def materialize_icon(url: str, dest_png: Path) -> Path | None:
    """Fetch URL and write dest_png as a PNG when possible."""
    if not url:
        return None
    if dest_png.is_file() and dest_png.stat().st_size > 0:
        return dest_png
    fetched = _fetch_url(url)
    if fetched is None:
        return None
    data, ctype = fetched
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    if _rasterize_to_png(data, ctype, url, dest_png):
        return dest_png
    # Last resort: keep original bytes with a matching suffix
    ext = dest_png.suffix or ".bin"
    if ctype == "image/svg+xml" or url.lower().endswith(".svg") or data[:200].lstrip().startswith(b"<svg"):
        ext = ".svg"
    elif ctype == "image/png" or url.lower().endswith(".png"):
        ext = ".png"
    elif "icon" in ctype or url.lower().endswith(".ico"):
        ext = ".ico"
    fallback = dest_png.with_suffix(ext)
    fallback.write_bytes(data)
    return fallback if fallback.stat().st_size > 0 else None


def download_icon(url: str) -> Path | None:
    """Fetch URL into the user cache; rasterize SVG → PNG when possible."""
    if not url:
        return None
    existing = cached_icon_path(url)
    if existing is not None:
        return existing
    path = _cache_path_for_url(url)
    return materialize_icon(url, path.with_suffix(".png"))


def fetch_icon_async(url: str, on_done: Callable[[Path | None], None]) -> None:
    """Download in a worker thread; on_done is invoked from that thread."""
    if not url:
        on_done(None)
        return
    hit = cached_icon_path(url)
    if hit is not None:
        on_done(hit)
        return

    with _LOCK:
        if url in _INFLIGHT:
            # Another fetch in progress — small wait loop in worker
            pass
        else:
            _INFLIGHT.add(url)

    def worker() -> None:
        try:
            path = download_icon(url)
            on_done(path)
        finally:
            with _LOCK:
                _INFLIGHT.discard(url)

    threading.Thread(target=worker, name="urstack-icon", daemon=True).start()
