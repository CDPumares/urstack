#!/usr/bin/env python3
"""Download app metadata into data/catalog/metadata.json.

Sources, in order:
  1. Flathub AppStream (Flatpak id or icon-map twin)
  2. Flathub search by app name
  3. Wikipedia summary for well-known vendor apps
  4. Catalog summary + homepage

  python3 scripts/vendor-app-metadata.py
  python3 scripts/vendor-app-metadata.py --force
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog"
OUT = CATALOG / "metadata.json"
CORE = ROOT / "lib" / "core"

sys.path.insert(0, str(CORE))
import app_meta  # noqa: E402

# Catalog id → Wikipedia article title for vendor apps with no Flathub page.
WIKI_TITLES: dict[str, str] = {
    "skype": "Skype",
    "webex": "Webex",
    "apple-music": "Apple Music",
    "davinci-resolve": "DaVinci Resolve",
    "cinebench": "Cinebench",
    "notion-prod": "Notion (productivity software)",
    "notion": "Notion (productivity software)",
    "evernote": "Evernote",
    "cursor": "Cursor (code editor)",
    "cursor-direct": "Cursor (code editor)",
    "jetbrains-toolbox": "JetBrains",
    "toolbox-direct": "JetBrains",
    "docker-desktop": "Docker (software)",
    "ollama": "Ollama",
    "ollama-direct": "Ollama",
    "ngrok": "Ngrok",
    "teamviewer": "TeamViewer",
    "balena-etcher": "Etcher (software)",
    "affinity": "Affinity (software)",
    "figma-linux": "Figma (software)",
    "fusion360": "Autodesk Fusion",
    "tailscale": "Tailscale",
    "nordvpn": "NordVPN",
    "mullvad-vpn": "Mullvad",
    "virtualbox": "VirtualBox",
    "veracrypt": "VeraCrypt",
    "battlenet": "Battle.net",
    "ea-app": "EA app",
    "roblox": "Roblox",
    "purge-shutter-encoder": "Shutter Encoder",
}

DEVELOPERS: dict[str, str] = {
    "skype": "Microsoft",
    "webex": "Cisco",
    "apple-music": "Apple",
    "davinci-resolve": "Blackmagic Design",
    "cinebench": "Maxon",
    "notion-prod": "Notion Labs",
    "notion": "Notion Labs",
    "evernote": "Evernote",
    "cursor": "Anysphere",
    "cursor-direct": "Anysphere",
    "jetbrains-toolbox": "JetBrains",
    "toolbox-direct": "JetBrains",
    "docker-desktop": "Docker, Inc.",
    "ollama": "Ollama",
    "ollama-direct": "Ollama",
    "ngrok": "ngrok",
    "teamviewer": "TeamViewer",
    "balena-etcher": "Balena",
    "affinity": "Serif",
    "figma-linux": "Figma",
    "fusion360": "Autodesk",
    "tailscale": "Tailscale",
    "nordvpn": "Nord Security",
    "mullvad-vpn": "Mullvad",
    "virtualbox": "Oracle",
    "veracrypt": "IDRIX",
    "battlenet": "Blizzard Entertainment",
    "ea-app": "Electronic Arts",
    "roblox": "Roblox Corporation",
    "purge-shutter-encoder": "Paul Pacifico",
}


def iter_catalog_apps() -> list[dict[str, str]]:
    apps: list[dict[str, str]] = []
    seen: set[str] = set()
    primary = CATALOG / "apps.json"
    if not primary.is_file():
        return apps
    try:
        data = json.loads(primary.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return apps
    for cat in data.get("categories") or []:
        for app in cat.get("apps") or []:
            if app.get("store") == "windows":
                continue
            aid = str(app.get("id") or "").strip()
            if not aid or aid in seen:
                continue
            seen.add(aid)
            apps.append(
                {
                    "id": aid,
                    "name": str(app.get("name") or ""),
                    "summary": str(app.get("summary") or ""),
                    "method": str(app.get("method") or ""),
                    "package": str(app.get("package") or ""),
                    "url": str(app.get("url") or ""),
                }
            )
    return apps


def fetch_via_flathub(row: dict[str, str], flathub_id: str) -> dict | None:
    appstream = app_meta.fetch_appstream(flathub_id)
    if not appstream:
        return None
    return app_meta.normalize_appstream(
        appstream, catalog_id=row["id"], flathub_id=flathub_id
    )


def fetch_one(row: dict[str, str], force: bool) -> tuple[str, dict | None, str]:
    aid = row["id"]
    if not force:
        existing = app_meta.load_bundled_meta(CATALOG).get(aid)
        if existing and (existing.get("description") or existing.get("screenshots")):
            return aid, existing, "skip"

    flathub_id = app_meta.flathub_id_for_row(row, CATALOG)
    if flathub_id:
        meta = fetch_via_flathub(row, flathub_id)
        if meta:
            return aid, meta, "ok"

    hit = app_meta.search_flathub(row.get("name") or aid)
    if hit:
        ident = str(hit.get("app_id") or hit.get("id") or "")
        if ident:
            meta = fetch_via_flathub(row, ident)
            if meta:
                return aid, meta, "search"

    wiki_title = WIKI_TITLES.get(aid) or (row.get("name") or "")
    wiki = app_meta.fetch_wikipedia_summary(wiki_title) if wiki_title else None
    meta = app_meta.catalog_fallback_meta(
        row,
        wiki=wiki,
        developer=DEVELOPERS.get(aid, ""),
    )
    if meta.get("description"):
        return aid, meta, "wiki" if wiki else "catalog"
    return aid, meta, "catalog"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-download apps that already have metadata")
    parser.add_argument("--jobs", type=int, default=6, help="Parallel fetches (default 6)")
    args = parser.parse_args()

    rows = iter_catalog_apps()
    if not rows:
        print("no catalog apps found", file=sys.stderr)
        return 1

    previous = app_meta.load_bundled_meta(CATALOG)
    merged: dict[str, dict] = dict(previous) if not args.force else {}
    counts = {"ok": 0, "search": 0, "wiki": 0, "catalog": 0, "skip": 0, "fail": 0}

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(fetch_one, row, args.force) for row in rows]
        for fut in as_completed(futs):
            app_id, meta, status = fut.result()
            counts[status] = counts.get(status, 0) + 1
            if meta:
                merged[app_id] = meta
            if status not in {"skip"}:
                print(f"{status:<8} {app_id}")

    payload = {
        "version": 1,
        "fetched": date.today().isoformat(),
        "source": "flathub-appstream, flathub-search, wikipedia, catalog",
        "apps": dict(sorted(merged.items())),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT}  stored {len(merged)}  "
        + "  ".join(f"{k} {v}" for k, v in counts.items() if v)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
