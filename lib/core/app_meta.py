#!/usr/bin/env python3
"""Catalog app metadata: Flathub AppStream, bundled JSON, on-demand cache.

Stored fields are compact (description, developer, license, links, screenshot
URLs). Screenshot images are fetched into the user cache when a details page
opens — they are too large to vendor next to the repo.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections.abc import Callable

_CTX = ssl.create_default_context()
_UA = "UrStack/1.0 (app-meta)"
_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "stackup" / "app-meta"
_APP_ROOT = Path(__file__).resolve().parents[2]
_META: dict[str, dict] | None = None
_ICON_IDS: dict[str, str] | None = None
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()

FLATHUB_APPSTREAM = "https://flathub.org/api/v2/appstream/{app_id}?locale=en"


def meta_cache_dir() -> Path:
    _CACHE.mkdir(parents=True, exist_ok=True)
    return _CACHE


def shot_cache_dir() -> Path:
    d = meta_cache_dir() / "shots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_stem(app_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (app_id or "").strip())
    return stem.strip("._") or "app"


def _catalog_dirs(catalog_dir: Path | None = None) -> list[Path]:
    out: list[Path] = []
    if catalog_dir is not None:
        out.append(catalog_dir)
    out.append(_APP_ROOT / "data" / "catalog")
    env_root = os.environ.get("STACKUP_ROOT") or os.environ.get("FEDORA_UPDATES_ROOT")
    if env_root:
        out.append(Path(env_root) / "data" / "catalog")
    seen: set[str] = set()
    unique: list[Path] = []
    for path in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def html_to_text(raw: str) -> str:
    """Turn AppStream HTML into readable wrapped plain text."""
    text = raw or ""
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n\n", text)
    text = re.sub(r"(?is)<p(\s[^>]*)?>", "", text)
    text = re.sub(r"(?is)<li(\s[^>]*)?>", "• ", text)
    text = re.sub(r"(?is)</li\s*>", "\n", text)
    text = re.sub(r"(?is)</?(ul|ol)[^>]*>", "\n", text)
    text = re.sub(r"(?is)</h[1-6]>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_FLATHUB_ICON_RE = re.compile(
    r"dl\.flathub\.org/media/icons/\d+x\d+/([^/]+)\.(?:png|svg)$",
    re.I,
)


def flathub_id_from_icon_url(url: str) -> str:
    match = _FLATHUB_ICON_RE.search(url or "")
    if not match:
        return ""
    ident = urllib.parse.unquote(match.group(1))
    return ident if looks_like_flatpak_id(ident) else ""


def looks_like_flatpak_id(value: str) -> bool:
    raw = (value or "").strip()
    return bool(raw) and raw.count(".") >= 2 and "://" not in raw and " " not in raw


def pick_shot_urls(shot: dict) -> tuple[str, str]:
    """Return (thumb_url, full_url) from a Flathub screenshot object."""
    sizes = shot.get("sizes") or []
    scored: list[tuple[int, str]] = []
    for item in sizes:
        if not isinstance(item, dict):
            continue
        src = (item.get("src") or "").strip()
        if not src:
            continue
        try:
            width = int(item.get("width") or 0)
        except (TypeError, ValueError):
            width = 0
        scored.append((width, src))
    if not scored:
        return "", ""
    scored.sort()
    thumb = min(scored, key=lambda t: abs(t[0] - 640) if t[0] else 10_000)[1]
    full = scored[-1][1]
    return thumb, full


def normalize_appstream(
    appstream: dict,
    *,
    catalog_id: str,
    flathub_id: str,
) -> dict:
    """Shrink Flathub AppStream JSON into the fields the details page needs."""
    urls = appstream.get("urls") if isinstance(appstream.get("urls"), dict) else {}
    shots: list[dict[str, str]] = []
    for shot in (appstream.get("screenshots") or [])[:6]:
        if not isinstance(shot, dict):
            continue
        thumb, full = pick_shot_urls(shot)
        if thumb or full:
            shots.append({"thumb": thumb or full, "full": full or thumb})
    desc = html_to_text(str(appstream.get("description") or ""))
    if len(desc) > 4000:
        desc = desc[:3990].rsplit(" ", 1)[0] + "…"
    releases = appstream.get("releases") or []
    version = ""
    if isinstance(releases, list) and releases and isinstance(releases[0], dict):
        version = str(releases[0].get("version") or "").strip()
    extra = appstream.get("metadata") if isinstance(appstream.get("metadata"), dict) else {}
    verified_raw = extra.get("flathub::verification::verified")
    verified = str(verified_raw).lower() in {"true", "1", "yes"}
    cats = [
        c
        for c in (appstream.get("categories") or [])
        if isinstance(c, str) and c.strip()
    ][:8]
    return {
        "id": catalog_id,
        "source": "flathub",
        "flathub_id": flathub_id,
        "name": str(appstream.get("name") or "").strip(),
        "summary": html_to_text(str(appstream.get("summary") or "")),
        "description": desc,
        "developer": str(appstream.get("developer_name") or "").strip(),
        "license": str(appstream.get("project_license") or "").strip(),
        "version": version,
        "homepage": str(urls.get("homepage") or "").strip(),
        "donation": str(urls.get("donation") or "").strip(),
        "bugtracker": str(urls.get("bugtracker") or "").strip(),
        "help": str(urls.get("help") or "").strip(),
        "categories": cats,
        "verified": verified,
        "screenshots": shots,
    }


def load_icon_ids(catalog_dir: Path | None = None) -> dict[str, str]:
    """catalog id → Flathub app id from icon-map.json."""
    global _ICON_IDS
    if _ICON_IDS is not None and catalog_dir is None:
        return _ICON_IDS
    mapping: dict[str, str] = {}
    for base in _catalog_dirs(catalog_dir):
        path = base / "icon-map.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for app_id, meta in (data.get("icons") or {}).items():
            if isinstance(meta, dict):
                icon_id = str(meta.get("icon_id") or "").strip()
                if not looks_like_flatpak_id(icon_id):
                    icon_id = flathub_id_from_icon_url(str(meta.get("icon") or ""))
                if looks_like_flatpak_id(icon_id):
                    mapping[str(app_id)] = icon_id
            elif isinstance(meta, str):
                icon_id = flathub_id_from_icon_url(meta)
                if looks_like_flatpak_id(icon_id):
                    mapping[str(app_id)] = icon_id
        break
    if catalog_dir is None:
        _ICON_IDS = mapping
    return mapping


def load_bundled_meta(catalog_dir: Path | None = None) -> dict[str, dict]:
    """catalog id → metadata dict from data/catalog/metadata.json."""
    global _META
    if _META is not None and catalog_dir is None:
        return _META
    mapping: dict[str, dict] = {}
    for base in _catalog_dirs(catalog_dir):
        path = base / "metadata.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(apps, dict):
            continue
        for app_id, meta in apps.items():
            if isinstance(meta, dict):
                mapping[str(app_id)] = meta
        break
    if catalog_dir is None:
        _META = mapping
    return mapping


def cached_meta_path(catalog_id: str) -> Path:
    return meta_cache_dir() / f"{safe_stem(catalog_id)}.json"


def load_cached_meta(catalog_id: str) -> dict | None:
    path = cached_meta_path(catalog_id)
    if not path.is_file() or path.stat().st_size < 8:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_cached_meta(catalog_id: str, meta: dict) -> None:
    path = cached_meta_path(catalog_id)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def flathub_id_for_row(row: dict[str, str], catalog_dir: Path | None = None) -> str:
    package = (row.get("package") or "").strip()
    if (row.get("method") or "") == "flatpak" and looks_like_flatpak_id(package):
        return package
    mapped = load_icon_ids(catalog_dir).get(row.get("id") or "")
    if mapped:
        return mapped
    if looks_like_flatpak_id(package):
        return package
    return ""


def meta_for_row(row: dict[str, str], catalog_dir: Path | None = None) -> dict | None:
    """Return bundled or previously cached metadata, if any."""
    aid = (row.get("id") or "").strip()
    if aid:
        bundled = load_bundled_meta(catalog_dir).get(aid)
        if bundled:
            return bundled
        cached = load_cached_meta(aid)
        if cached:
            return cached
    return None


def fetch_json(url: str, timeout: int = 25, headers: dict[str, str] | None = None) -> dict | None:
    hdrs = {"user-agent": _UA, "accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def fetch_bytes(url: str, timeout: int = 25) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"user-agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    return data or None


def fetch_appstream(flathub_id: str) -> dict | None:
    if not looks_like_flatpak_id(flathub_id):
        return None
    ident = urllib.parse.quote(flathub_id, safe=".")
    return fetch_json(FLATHUB_APPSTREAM.format(app_id=ident))


def names_close(left: str, right: str) -> bool:
    def norm(s: str) -> str:
        s = re.sub(r"\([^)]*\)", "", s or "")
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    a, b = norm(left), norm(right)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def search_flathub(query: str) -> dict | None:
    """Return the best Flathub search hit for a name, or None."""
    q = (query or "").strip()
    if len(q) < 2:
        return None
    body = json.dumps({"query": q, "locale": "en"}).encode("utf-8")
    req = urllib.request.Request(
        "https://flathub.org/api/v2/search",
        data=body,
        method="POST",
        headers={
            "user-agent": _UA,
            "accept": "application/json",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    hits = data if isinstance(data, list) else (data.get("hits") if isinstance(data, dict) else None)
    if not isinstance(hits, list):
        return None
    for hit in hits[:8]:
        if not isinstance(hit, dict):
            continue
        name = str(hit.get("name") or "")
        ident = str(hit.get("app_id") or hit.get("id") or "")
        if looks_like_flatpak_id(ident) and names_close(q, name):
            return hit
    return None


def fetch_wikipedia_summary(title: str) -> dict | None:
    """Plain-language extract from Wikipedia REST, or None."""
    title = (title or "").strip()
    if not title:
        return None
    slug = urllib.parse.quote(title.replace(" ", "_"))
    data = fetch_json(
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}",
        headers={
            "user-agent": "UrStack/1.0 (https://github.com/; Fedora catalog metadata)",
            "accept": "application/json",
        },
    )
    if not data or data.get("type") == "disambiguation":
        return None
    extract = html_to_text(str(data.get("extract") or ""))
    if not extract:
        return None
    urls = data.get("content_urls") if isinstance(data.get("content_urls"), dict) else {}
    desktop = urls.get("desktop") if isinstance(urls.get("desktop"), dict) else {}
    return {
        "extract": extract,
        "description": html_to_text(str(data.get("description") or "")),
        "homepage": str(desktop.get("page") or "").strip(),
        "title": str(data.get("title") or title),
    }


def catalog_fallback_meta(
    row: dict[str, str],
    *,
    wiki: dict | None = None,
    developer: str = "",
) -> dict:
    """Metadata for vendor apps with no Flathub listing."""
    summary = (row.get("summary") or "").strip()
    desc = (wiki or {}).get("extract") or summary
    homepage = (row.get("url") or "").strip() or str((wiki or {}).get("homepage") or "")
    return {
        "id": (row.get("id") or "").strip(),
        "source": "wikipedia" if wiki and wiki.get("extract") else "catalog",
        "flathub_id": "",
        "name": (row.get("name") or "").strip(),
        "summary": (wiki or {}).get("description") or summary,
        "description": desc,
        "developer": developer,
        "license": "",
        "version": "",
        "homepage": homepage,
        "donation": "",
        "bugtracker": "",
        "help": "",
        "categories": [],
        "verified": False,
        "screenshots": [],
    }


def download_meta_for_row(row: dict[str, str], catalog_dir: Path | None = None) -> dict | None:
    """Fetch Flathub AppStream and cache it. Returns normalized metadata or None."""
    existing = meta_for_row(row, catalog_dir)
    if existing and (existing.get("description") or existing.get("screenshots")):
        return existing
    flathub_id = flathub_id_for_row(row, catalog_dir)
    if not flathub_id:
        return existing
    appstream = fetch_appstream(flathub_id)
    if not appstream:
        return existing
    catalog_id = (row.get("id") or flathub_id).strip()
    meta = normalize_appstream(appstream, catalog_id=catalog_id, flathub_id=flathub_id)
    if catalog_id:
        try:
            save_cached_meta(catalog_id, meta)
        except OSError:
            pass
    return meta


def fetch_meta_async(
    row: dict[str, str],
    on_done: Callable[[dict | None], None],
    catalog_dir: Path | None = None,
) -> None:
    """Resolve metadata off the UI thread. on_done runs on the worker thread."""
    local = meta_for_row(row, catalog_dir)
    if local is not None:
        on_done(local)
        if local.get("description") or local.get("screenshots"):
            return
    key = (row.get("id") or row.get("package") or "").strip()
    if not key:
        on_done(local)
        return

    with _LOCK:
        if key in _INFLIGHT:
            on_done(local)
            return
        _INFLIGHT.add(key)

    def worker() -> None:
        try:
            on_done(download_meta_for_row(row, catalog_dir))
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    threading.Thread(target=worker, name="urstack-meta", daemon=True).start()


def cached_shot_path(url: str) -> Path | None:
    if not url:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    path = shot_cache_dir() / f"{digest}.png"
    if path.is_file() and path.stat().st_size > 0:
        return path
    return None


def download_shot(url: str) -> Path | None:
    if not url:
        return None
    hit = cached_shot_path(url)
    if hit is not None:
        return hit
    data = fetch_bytes(url)
    if not data:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    path = shot_cache_dir() / f"{digest}.png"
    path.write_bytes(data)
    return path if path.stat().st_size > 0 else None


def fetch_shot_async(url: str, on_done: Callable[[Path | None], None]) -> None:
    if not url:
        on_done(None)
        return
    hit = cached_shot_path(url)
    if hit is not None:
        on_done(hit)
        return

    def worker() -> None:
        on_done(download_shot(url))

    threading.Thread(target=worker, name="urstack-shot", daemon=True).start()
