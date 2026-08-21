#!/usr/bin/env python3
"""Download catalog logos into data/catalog/icons/{app_id}.png.

Reads URLs from data/catalog/icon-map.json (and Flatpak package ids in the
catalog) and writes PNGs the Apps page loads from the repo — no runtime
download for mapped apps.

  python3 scripts/vendor-app-icons.py
  python3 scripts/vendor-app-icons.py --force   # re-download everything
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog"
OUT = CATALOG / "icons"
CORE = ROOT / "lib" / "core"

sys.path.insert(0, str(CORE))
import app_icons  # noqa: E402


def load_targets() -> dict[str, str]:
    """app_id → icon URL."""
    mapping: dict[str, str] = {}
    map_path = CATALOG / "icon-map.json"
    if map_path.is_file():
        data = json.loads(map_path.read_text(encoding="utf-8"))
        for app_id, meta in (data.get("icons") or {}).items():
            if isinstance(meta, str) and meta.startswith("http"):
                mapping[app_id] = meta
            elif isinstance(meta, dict):
                url = (meta.get("icon") or "").strip()
                if url.startswith("http"):
                    mapping[app_id] = url
                elif meta.get("icon_id"):
                    mapping[app_id] = app_icons.flathub_icon_url(str(meta["icon_id"]))

    for path in sorted(CATALOG.glob("*.json")):
        if path.name == "icon-map.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for cat in data.get("categories", []):
            for app in cat.get("apps", []):
                if app.get("store") == "windows":
                    continue
                aid = (app.get("id") or "").strip()
                if not aid or aid in mapping:
                    continue
                if app.get("method") == "flatpak" and app.get("package"):
                    mapping[aid] = app_icons.flathub_icon_url(app["package"])
    return mapping


def fetch_one(app_id: str, url: str, force: bool) -> tuple[str, Path | None, str]:
    dest = OUT / f"{app_icons.safe_icon_stem(app_id)}.png"
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        return app_id, dest, "skip"
    if force and dest.exists():
        dest.unlink()
    cached = app_icons.cached_icon_path(url)
    if cached is not None and cached.suffix == ".png" and not force:
        dest.write_bytes(cached.read_bytes())
        return app_id, dest, "ok"
    path = app_icons.materialize_icon(url, dest)
    if path is None:
        return app_id, None, "fail"
    return app_id, path, "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-download icons that already exist")
    parser.add_argument("--jobs", type=int, default=8, help="Parallel downloads (default 8)")
    args = parser.parse_args()

    targets = load_targets()
    if not targets:
        print("no icon URLs found in data/catalog/", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    ok = skip = fail = 0
    missed: list[str] = []

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = [pool.submit(fetch_one, aid, url, args.force) for aid, url in sorted(targets.items())]
        for fut in as_completed(futs):
            app_id, path, status = fut.result()
            if status == "ok":
                ok += 1
                print(f"ok    {app_id}  {path.name}")
            elif status == "skip":
                skip += 1
            else:
                fail += 1
                missed.append(app_id)
                print(f"fail  {app_id}", file=sys.stderr)

    print(f"wrote {OUT}  ok {ok}  skipped {skip}  fail {fail}  total {len(targets)}")
    if missed:
        print("missing:", ", ".join(missed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
