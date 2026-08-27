#!/usr/bin/env python3
"""GitHub theme picks plus the GNOME Look / KDE Look catalog.

Featured packs download from GitHub releases. The rest of Browse themes is
the openDesktop / OCS catalog (screenshots included). Install unpacks a free
archive user-local; installer scripts inside a zip are never executed.
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
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
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
    Category("all", "All"),
    Category("looks", "Desktop looks"),
    Category("gtk", "GTK themes"),
    Category("plasma", "Plasma", ("plasma",)),
    Category("icons", "Icons"),
    Category("cursors", "Cursors"),
)

HOSTS: dict[str, dict[str, str]] = {
    "gnome-look": {
        "api": "https://api.gnome-look.org/ocs/v1",
        "label": "GNOME Look",
        "site": "https://www.gnome-look.org",
    },
    "kde-look": {
        "api": "https://api.kde-look.org/ocs/v1",
        "label": "KDE Look",
        "site": "https://www.kde-look.org",
    },
    "xfce-look": {
        "api": "https://api.xfce-look.org/ocs/v1",
        "label": "XFCE Look",
        "site": "https://www.xfce-look.org",
    },
}

# openDesktop / OCS category ids (same feeds Discover uses, with screenshots).
OCS_CATEGORIES: dict[str, tuple[tuple[str, int], ...]] = {
    "all": (("gnome-look", 135), ("kde-look", 135)),
    "looks": (("gnome-look", 135), ("kde-look", 135)),
    "gtk": (("gnome-look", 135), ("kde-look", 135), ("xfce-look", 135)),
    "plasma": (("kde-look", 722),),
    "icons": (("gnome-look", 132), ("kde-look", 132)),
    "cursors": (("gnome-look", 107), ("kde-look", 107)),
}

OCS_PAGESIZE = 24

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
    if "all" in cats:
        return "all"
    return cats[0] if cats else "gtk"


def category_label(kind: str) -> str:
    cat = _CATEGORY_BY_ID.get(kind)
    return cat.label if cat else kind.replace("-", " ").title()


def github_opengraph_url(repo: str) -> str:
    if not _valid_github_repo(repo):
        return ""
    return f"https://opengraph.githubassets.com/urstack/{repo}"


def ocs_source_for(kind: str, desktop: str) -> tuple[str, int] | None:
    sources = OCS_CATEGORIES.get(kind) or OCS_CATEGORIES.get("gtk")
    if not sources:
        return None
    desk = (desktop or "unknown").strip().lower()
    if kind == "plasma" and desk not in {"plasma", "unknown"}:
        return None
    preferred = "kde-look" if desk == "plasma" else "gnome-look"
    for host, cid in sources:
        if host == preferred:
            return host, cid
    return sources[0]


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


def _ocs_data(payload: dict | list | None) -> list:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    ocs = payload.get("ocs")
    if data is None and isinstance(ocs, dict):
        data = ocs.get("data")
    if isinstance(data, dict):
        inner = data.get("content")
        data = inner if isinstance(inner, list) else [data]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_BR = re.compile(r"<br\s*/?>", re.I)


def strip_html(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    raw = _HTML_BR.sub("\n", raw)
    raw = re.sub(r"</p\s*>", "\n\n", raw, flags=re.I)
    raw = _HTML_TAG.sub("", raw)
    raw = (
        raw.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", raw)).strip()


def parse_screenshots(raw: dict) -> list[dict[str, str]]:
    """OCS previewpic1..N plus smallpreviewpic1..N, de-duplicated."""
    shots: list[dict[str, str]] = []
    seen: set[str] = set()
    for index in range(1, 9):
        full = str(raw.get(f"previewpic{index}") or "").strip()
        thumb = str(raw.get(f"smallpreviewpic{index}") or "").strip()
        url = full or thumb
        if not url or url in seen:
            continue
        seen.add(url)
        if thumb and thumb not in seen and thumb != url:
            seen.add(thumb)
        shots.append({"thumb": thumb or url, "full": full or url})
    return shots


def parse_detail(raw: dict) -> dict:
    """Normalize one OCS content/data/{id} payload for the theme detail page."""
    shots = parse_screenshots(raw)
    summary = str(raw.get("summary") or "").strip()
    desc = strip_html(str(raw.get("description") or ""))
    preview = ""
    if shots:
        preview = shots[0]["thumb"]
    else:
        preview = str(raw.get("smallpreviewpic1") or raw.get("previewpic1") or "").strip()
    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "summary": summary,
        "description": desc or summary,
        "author": str(raw.get("personid") or raw.get("username") or "").strip(),
        "license": str(raw.get("license") or "").strip(),
        "version": str(raw.get("version") or "").strip(),
        "downloads": str(raw.get("downloads") or "").strip(),
        "score": str(raw.get("score") or "").strip(),
        "typename": str(raw.get("typename") or "").strip(),
        "detailpage": str(raw.get("detailpage") or "").strip(),
        "preview": preview,
        "screenshots": shots,
    }


def source_label(row: dict[str, str]) -> str:
    host = (row.get("host") or "").strip()
    if host == "catalog":
        return "GitHub"
    return HOSTS.get(host, {}).get("label") or "GNOME Look"


def _shot_urls_from_row(row: dict[str, str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for blob in (row.get("shots") or "", row.get("preview") or ""):
        for part in str(blob).split("\n"):
            url = part.strip()
            if url and url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        og = github_opengraph_url(row.get("github") or "")
        if og:
            urls.append(og)
    return urls


def details_from_row(row: dict[str, str]) -> dict:
    """Details that are already on a catalog / list card — no network."""
    urls = _shot_urls_from_row(row)
    shots = [{"thumb": url, "full": url} for url in urls]
    summary = str(row.get("summary") or "").strip()
    desc = str(row.get("description") or "").strip() or summary
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "",
        "summary": summary,
        "description": desc,
        "author": row.get("author") or "",
        "license": row.get("license") or "",
        "version": row.get("version") or "",
        "downloads": row.get("downloads") or "",
        "score": row.get("score") or "",
        "typename": row.get("typename") or "",
        "detailpage": row.get("detailpage") or row.get("homepage") or "",
        "homepage": row.get("homepage") or row.get("detailpage") or "",
        "github": row.get("github") or "",
        "host": row.get("host") or "",
        "kind": row.get("kind") or "",
        "source": source_label(row),
        "featured": row.get("featured") or "",
        "preview": urls[0] if urls else "",
        "screenshots": shots,
    }


def fetch_details(row: dict[str, str]) -> dict:
    """List-row details, plus OCS screenshots / description when this is a store listing."""
    info = details_from_row(row)
    host = (row.get("host") or "").strip()
    cid = (row.get("id") or "").strip()
    if host not in HOSTS:
        return info
    try:
        parsed = parse_detail(fetch_item(host, cid))
    except ThemeStoreError:
        return info
    for key in (
        "description",
        "summary",
        "author",
        "license",
        "version",
        "downloads",
        "score",
        "typename",
        "detailpage",
        "preview",
    ):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            info[key] = value.strip()
    if parsed.get("screenshots"):
        info["screenshots"] = parsed["screenshots"]
    if parsed.get("detailpage"):
        info["homepage"] = parsed["detailpage"]
    return info


def fetch_details_async(row: dict[str, str], on_done: Callable[[dict], None]) -> None:
    snapshot = dict(row)

    def work() -> None:
        try:
            info = fetch_details(snapshot)
        except Exception:  # noqa: BLE001
            info = details_from_row(snapshot)
        on_done(info)

    threading.Thread(target=work, daemon=True, name="urstack-theme-detail").start()


def parse_list(payload: dict | list | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for raw in _ocs_data(payload):
        cid = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not cid or not name:
            continue
        shots = parse_screenshots(raw)
        items.append(
            {
                "id": cid,
                "name": name,
                "summary": str(raw.get("summary") or "").strip(),
                "description": strip_html(str(raw.get("description") or "")),
                "author": str(raw.get("personid") or raw.get("username") or "").strip(),
                "downloads": str(raw.get("downloads") or "0"),
                "score": str(raw.get("score") or "0"),
                "typename": str(raw.get("typename") or "").strip(),
                "preview": str(
                    raw.get("smallpreviewpic1") or raw.get("previewpic1") or ""
                ).strip(),
                "shots": "\n".join(
                    str(s.get("full") or s.get("thumb") or "") for s in shots
                ).strip(),
                "detailpage": str(raw.get("detailpage") or "").strip(),
            }
        )
    return items


def _price_paid(raw: str) -> bool:
    text = (raw or "0").strip()
    if not text or text in {"0", "0.0", "0.00"}:
        return False
    try:
        return float(text) > 0
    except ValueError:
        return False


def pick_download(item: dict) -> tuple[str, str] | None:
    """First free archive file on an OCS detail payload, or None."""
    ranked: list[tuple[int, int, str, str]] = []
    for index in range(1, 16):
        link = str(item.get(f"downloadlink{index}") or "").strip()
        if not link:
            continue
        if _price_paid(str(item.get(f"downloadprice{index}") or "0")):
            continue
        name = str(item.get(f"downloadname{index}") or "").strip()
        tags = str(item.get(f"downloadtags{index}") or "")
        tags_l = tags.lower()
        if "text/html" in tags_l or "text/plain" in tags_l:
            continue
        rank = _archive_rank(name, tags)
        if rank <= 0:
            continue
        ranked.append((rank, index, link, name or f"theme-{index}.tar.xz"))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return ranked[0][2], ranked[0][3]


def list_url(host: str, category_id: int, *, search: str = "", page: int = 0) -> str:
    info = HOSTS.get(host)
    if info is None:
        raise ThemeStoreError(f"Unknown theme store {host}")
    query = {
        "categories": str(category_id),
        "sortmode": "high",
        "pagesize": str(OCS_PAGESIZE),
        "page": str(max(0, int(page))),
        "format": "json",
    }
    if search.strip():
        query["search"] = search.strip()
    return f"{info['api']}/content/data?{urllib.parse.urlencode(query)}"


def fetch_item(host: str, content_id: str) -> dict:
    info = HOSTS.get(host)
    if info is None:
        raise ThemeStoreError(f"Unknown theme store {host}")
    cid = (content_id or "").strip()
    if not cid or not re.fullmatch(r"[0-9]+", cid):
        raise ThemeStoreError("Invalid listing id")
    url = f"{info['api']}/content/data/{urllib.parse.quote(cid)}?format=json"
    payload = fetch_json(url, timeout=30)
    items = _ocs_data(payload)
    if not items:
        raise ThemeStoreError(f"Could not load this listing from {info['label']}")
    return items[0]


def list_ocs(kind: str, desktop: str, *, search: str = "") -> tuple[list[dict[str, str]], str]:
    src = ocs_source_for(kind, desktop)
    if src is None:
        return [], ""
    host, category_id = src
    label = HOSTS[host]["label"]
    payload = fetch_json(list_url(host, category_id, search=search))
    if payload is None:
        return [], label
    rows = parse_list(payload)
    for row in rows:
        row["host"] = host
        row["kind"] = kind
        row["license"] = ""
        row["github"] = ""
    return rows, label


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


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
        shot_urls: list[str] = []
        extra_shots = raw.get("screenshots") or []
        if isinstance(extra_shots, str):
            extra_shots = extra_shots.split("\n")
        if isinstance(extra_shots, list):
            for item in extra_shots:
                if isinstance(item, str):
                    url = item.strip()
                elif isinstance(item, dict):
                    url = str(item.get("full") or item.get("thumb") or item.get("url") or "").strip()
                else:
                    url = ""
                if url.startswith(("http://", "https://")):
                    shot_urls.append(url)
        preview = str(raw.get("preview") or "").strip() or github_opengraph_url(repo)
        homepage = str(raw.get("homepage") or f"https://github.com/{repo}").strip()
        themes.append(
            {
                "id": cid,
                "name": name,
                "summary": str(raw.get("summary") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
                "author": str(raw.get("author") or repo.split("/")[0]),
                "license": str(raw.get("license") or "").strip(),
                "kinds": kinds_s,
                "desktops": desks_s,
                "github": repo,
                "asset": str(raw.get("asset") or "").strip(),
                "ref": str(raw.get("ref") or "").strip(),
                "ref_kind": str(raw.get("ref_kind") or "").strip(),
                "homepage": homepage,
                "preview": preview,
                "shots": "\n".join(shot_urls),
                "host": "catalog",
                "detailpage": homepage,
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
    if kind and kind not in {"all", ""} and kind not in kinds:
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
    include_ocs: bool = True,
) -> tuple[list[dict[str, str]], str]:
    featured = [
        dict(row)
        for row in load_catalog(catalog)
        if row_visible(row, kind, desktop, search)
    ]
    for row in featured:
        row["kind"] = kind
        row["typename"] = category_label(kind)
        row["featured"] = "1"
    seen = {_norm_name(row.get("name") or "") for row in featured}
    extra: list[dict[str, str]] = []
    ocs_label = ""
    if include_ocs:
        ocs_rows, ocs_label = list_ocs(kind, desktop, search=search)
        for row in ocs_rows:
            key = _norm_name(row.get("name") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            extra.append(row)
    label = "GitHub picks"
    if ocs_label and extra:
        label = f"GitHub picks and {ocs_label}"
    elif ocs_label and not featured:
        label = ocs_label
    return featured + extra, label


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
    """Download a GitHub pick or GNOME/KDE Look listing and install it user-local."""
    import look as look_engine

    host_id = (host or "catalog").strip()
    _progress("Looking up the pack…", 4)
    if host_id in HOSTS:
        item = fetch_item(host_id, content_id)
        picked = pick_download(item)
        if picked is None:
            raise ThemeStoreError("No free theme archive on this listing")
        url, name = picked
        title = str(item.get("name") or name)
    elif host_id in {"catalog", "github"}:
        entry = catalog_entry(content_id)
        if entry is None:
            raise ThemeStoreError("Unknown theme in the catalog")
        url, name = resolve_download(entry)
        title = entry["name"]
    else:
        raise ThemeStoreError("Unknown theme source")
    filename = safe_filename(name)
    _progress(f"Downloading {title}…", 12)
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
