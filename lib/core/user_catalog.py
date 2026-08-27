"""Personal Apps overlay (~/.config/urstack/catalog-user.json).

Users may add Flathub IDs, DNF package names, and Snap names. Extra yum/COPR/
Flatpak remotes, vendor scripts, and arbitrary RPM URLs are refused.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ALLOWED_METHODS = frozenset({"flatpak", "dnf", "snap"})
METHOD_LABELS = {
    "flatpak": "Flathub",
    "dnf": "DNF",
    "snap": "Snap",
}
MAX_APPS = 300
MAX_NAME = 80
MAX_PACKAGE = 255

# Same rule as catalog.sh _catalog_valid_pkg_name (dnf/snap, and dotted ids).
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# Reverse-DNS Flathub id: at least two dots' worth of segments (org.foo.Bar).
FLATPAK_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*){2,}$"
)
USER_ID_RE = re.compile(r"^user-[A-Za-z0-9][A-Za-z0-9._+-]*$")
UNSAFE_NAME_RE = re.compile(r"[\n\r|]")


def user_catalog_path(config_dir: Path | str | None = None) -> Path:
    if config_dir:
        return Path(config_dir) / "catalog-user.json"
    env_dir = os.environ.get("FEDORA_UPDATES_CONFIG_DIR", "").strip()
    if env_dir:
        return Path(env_dir) / "catalog-user.json"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "urstack" / "catalog-user.json"


def valid_package(method: str, package: str) -> bool:
    if not package or len(package) > MAX_PACKAGE:
        return False
    if method == "flatpak":
        return bool(FLATPAK_RE.fullmatch(package))
    if method in {"dnf", "snap"}:
        return bool(PKG_RE.fullmatch(package))
    return False


def default_name(package: str) -> str:
    tail = package.rsplit(".", 1)[-1] if "." in package else package
    return tail.replace("-", " ").replace("_", " ").strip() or package


def make_id(method: str, package: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", package).strip("-").lower() or "app"
    return f"user-{method}-{slug}"[:120]


def clean_name(value: object, limit: int = MAX_NAME) -> str:
    text = UNSAFE_NAME_RE.sub(" ", str(value or "")).strip()
    return text[:limit].strip()


def _iter_raw_apps(data: object) -> list[dict]:
    if not isinstance(data, dict):
        return []
    apps = data.get("apps")
    if isinstance(apps, list) and apps:
        return [a for a in apps if isinstance(a, dict)]
    out: list[dict] = []
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        for app in cat.get("apps") or []:
            if isinstance(app, dict):
                out.append(app)
    return out


def normalize_app(raw: dict) -> dict[str, str] | None:
    method = str(raw.get("method") or "").strip().lower()
    package = str(raw.get("package") or "").strip()
    if method not in ALLOWED_METHODS or not valid_package(method, package):
        return None
    name = clean_name(raw.get("name") or "") or default_name(package)
    aid = str(raw.get("id") or "").strip()
    if not USER_ID_RE.fullmatch(aid):
        aid = make_id(method, package)
    summary = clean_name(raw.get("summary") or "", 160) or (
        f"Added by you · {METHOD_LABELS[method]}"
    )
    return {
        "id": aid,
        "name": name,
        "summary": summary,
        "method": method,
        "package": package,
    }


def load_apps(path: Path | str | None = None) -> list[dict[str, str]]:
    dest = Path(path) if path else user_catalog_path()
    if not dest.is_file():
        return []
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    seen_ids: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for raw in _iter_raw_apps(data):
        app = normalize_app(raw)
        if app is None:
            continue
        key = (app["method"], app["package"])
        if app["id"] in seen_ids or key in seen_keys:
            continue
        seen_ids.add(app["id"])
        seen_keys.add(key)
        out.append(app)
        if len(out) >= MAX_APPS:
            break
    return out


def save_apps(apps: list[dict[str, str]], path: Path | str | None = None) -> None:
    dest = Path(path) if path else user_catalog_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "apps": [normalize_app(a) for a in apps]}
    payload["apps"] = [a for a in payload["apps"] if a]
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)


def add_app(
    method: str,
    package: str,
    name: str = "",
    *,
    path: Path | str | None = None,
    existing_packages: set[str] | None = None,
) -> dict[str, str]:
    method = (method or "").strip().lower()
    package = (package or "").strip()
    dest = Path(path) if path else user_catalog_path()
    if method not in ALLOWED_METHODS:
        raise ValueError(
            "Choose Flathub, DNF, or Snap. Extra remotes and vendor scripts are not allowed."
        )
    if not valid_package(method, package):
        if method == "flatpak":
            raise ValueError("Use a Flathub app ID like org.mozilla.firefox.")
        raise ValueError("Package names can only use letters, numbers, and . _ + -")
    if existing_packages and package in existing_packages:
        raise ValueError("That package is already in the catalog.")
    apps = load_apps(dest)
    if len(apps) >= MAX_APPS:
        raise ValueError(f"My apps is full ({MAX_APPS}). Remove one first.")
    for existing in apps:
        if existing["package"] == package and existing["method"] == method:
            raise ValueError("That app is already in My apps.")
        if existing["package"] == package:
            raise ValueError("That package is already in My apps.")
    app = normalize_app({"method": method, "package": package, "name": name})
    if app is None:
        raise ValueError("Could not add that app.")
    apps.append(app)
    save_apps(apps, dest)
    return app


def remove_app(app_id: str, path: Path | str | None = None) -> bool:
    aid = (app_id or "").strip()
    if not aid.startswith("user-"):
        return False
    dest = Path(path) if path else user_catalog_path()
    apps = load_apps(dest)
    kept = [a for a in apps if a["id"] != aid]
    if len(kept) == len(apps):
        return False
    save_apps(kept, dest)
    return True


def as_catalog_row(app: dict[str, str]) -> dict[str, str]:
    method = app.get("method") or "flatpak"
    package = app.get("package") or ""
    icon = ""
    if method == "flatpak" and package:
        icon = f"https://dl.flathub.org/media/icons/128x128/{package}.png"
    return {
        "id": app.get("id") or "",
        "name": app.get("name") or default_name(package),
        "summary": app.get("summary") or f"Added by you · {METHOD_LABELS.get(method, method)}",
        "category": "Added by you",
        "category_id": "added",
        "method": method,
        "package": package,
        "installed": "0",
        "url": "",
        "badge": method,
        "icon": icon,
        "repo_hint": "",
        "user": "1",
    }
