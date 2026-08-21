#!/usr/bin/env python3
"""Build data/catalog/icon-map.json — real logos for Apps list entries.

Sources (in order):
  1. Flatpak package id → Flathub PNG
  2. Curated Flathub ids / Simple Icons / site favicons
  3. Exact Flathub name match (no fuzzy — avoids wrong logos)
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog"
OUT = CATALOG / "icon-map.json"
CTX = ssl.create_default_context()
UA = {"user-agent": "UrStack/1.0 (+https://github.com/local/stackup)", "content-type": "application/json"}
SI = "https://cdn.jsdelivr.net/npm/simple-icons@v13/icons"
FH = "https://dl.flathub.org/media/icons/128x128"
FV = "https://www.google.com/s2/favicons?domain={domain}&sz=128"


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\([^)]*\)", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def flathub(app_id: str) -> str:
    return f"{FH}/{app_id}.png"


def simple(slug: str) -> str:
    return f"{SI}/{slug}.svg"


def favicon(domain: str) -> str:
    return FV.format(domain=domain)


# catalog id / normalized name / package → icon URL or flathub app id
CURATED: dict[str, str] = {
    # Flathub ids (resolved to PNG below)
    "chrome": "com.google.Chrome",
    "googlechrome": "com.google.Chrome",
    "edge": "com.microsoft.Edge",
    "microsoftedge": "com.microsoft.Edge",
    "opera": "com.opera.Opera",
    "floorp": "one.ablaze.floorp",
    "tor-browser": "org.torproject.torbrowser-launcher",
    "torbrowser": "org.torproject.torbrowser-launcher",
    "mullvad-browser": "net.mullvad.MullvadBrowser",
    "mullvadbrowser": "net.mullvad.MullvadBrowser",
    "whatsapp": "com.rtosta.zapzap",
    "whatsappdesktop": "com.rtosta.zapzap",
    "zapzap": "com.rtosta.zapzap",
    "microsoftteams": "com.github.IsmaelMartinez.teams_for_linux",
    "teams": "com.github.IsmaelMartinez.teams_for_linux",
    "threema": "ch.threema.threema-web-desktop",
    "viber": "com.viber.Viber",
    "tidal": "com.mastermindzh.tidal-hifi",
    "plexamp": "com.plexamp.Plexamp",
    "plex-desktop": "tv.plex.PlexDesktop",
    "plexdesktop": "tv.plex.PlexDesktop",
    "plexmediaserver": "tv.plex.PlexDesktop",
    "todoist": "com.todoist.Todoist",
    "logseq": "com.logseq.Logseq",
    "zotero": "org.zotero.Zotero",
    "wpsoffice": "com.wps.Office",
    "masterpdfeditor": "net.code_industry.MasterPDFEditor",
    "sublimetext": "com.sublimehq.SublimeText",
    "warp": "dev.warp.Warp",
    "warpterminal": "dev.warp.Warp",
    "gitkraken": "com.axosoft.GitKraken",
    "lmstudio": "ai.lmstudio.lm-studio",
    "1password": "com.onepassword.OnePassword",
    "dropbox": "com.dropbox.Client",
    "anydesk": "com.anydesk.Anydesk",
    "rustdesk": "com.rustdesk.RustDesk",
    "osu": "sh.ppy.osu",
    "waterfox": "net.waterfox.waterfox",
    "teamspeak3": "com.teamspeak.TeamSpeak3",
    "teamspeak6": "com.teamspeak.TeamSpeak",
    "putty": "uk.org.greenend.chiark.sgtatham.putty",
    "wireshark": "org.wireshark.Wireshark",
    "jellyfinserver": "org.jellyfin.JellyfinServer",
    "parsec": "com.parsecgaming.parsec",
    "geforcenow": "io.github.hmlendea.geforcenow-electron",
    "itchioapp": "io.itch.itch",
    "itchio": "io.itch.itch",
    "minecraftlauncher": "org.prismlauncher.PrismLauncher",
    "neovim": "io.neovim.nvim",
    # Brand SVGs / favicons for apps without a trustworthy Flathub twin
    "cursor": simple("visualstudiocode"),  # closest widely available mark; overridden by favicon
    "nodejs": simple("nodedotjs"),
    "nodejslts": simple("nodedotjs"),
    "python3": simple("python"),
    "rust": simple("rust"),
    "go": simple("go"),
    "git": simple("git"),
    "githubcli": simple("github"),
    "pnpm": simple("pnpm"),
    "yarn": simple("yarn"),
    "cmake": simple("cmake"),
    "dockerdesktop": simple("docker"),
    "docker": simple("docker"),
    "ollama": favicon("ollama.com"),
    "lua": simple("lua"),
    "ruby": simple("ruby"),
    "uv": favicon("docs.astral.sh"),
    "lazygit": simple("git"),
    "starshipshellprompt": simple("starship"),
    "starship": simple("starship"),
    "ohmyposhprompt": favicon("ohmyposh.dev"),
    "fastnodemanager": simple("nodedotjs"),
    "jetbrainstoolbox": simple("jetbrains"),
    "jetbrains toolbox": simple("jetbrains"),
    "vagrant": simple("vagrant"),
    "nmap": favicon("nmap.org"),
    "7zip": simple("7zip"),
    "hugo": simple("hugo"),
    "nuget": simple("nuget"),
    "unitygameengine": simple("unity"),
    "ngrok": simple("ngrok"),
    "evernote": simple("evernote"),
    "applemusic": simple("applemusic"),
    "davinciresolve": favicon("www.blackmagicdesign.com"),
    "roblox": simple("roblox"),
    "robloxsobervinegar": simple("roblox"),
    "battlenet": simple("battledotnet"),
    "eaapp": simple("ea"),
    "itunes": simple("apple"),
    "googledrive": simple("googledrive"),
    "protondrive": simple("proton"),
    "protonauthenticator": simple("proton"),
    "netbird": favicon("netbird.io"),
    "ventoy": favicon("www.ventoy.net"),
    "tightvnc": favicon("www.tightvnc.com"),
    "affinity": favicon("affinity.serif.com"),
    "affinityphotodesigner": favicon("affinity.serif.com"),
    "figma": simple("figma"),
    "figma-linux": simple("figma"),
    "cinebench": favicon("www.maxon.net"),
    "cinebenchr23": favicon("www.maxon.net"),
    "notion": simple("notion"),
    "tailscale": simple("tailscale"),
    "nordvpn": simple("nordvpn"),
    "mullvadvpn": simple("mullvad"),
    "teamviewer": simple("teamviewer"),
    "virtualbox": simple("virtualbox"),
    "oraclevirtualbox": simple("virtualbox"),
    "veracrypt": favicon("www.veracrypt.fr"),
    "wireguard": simple("wireguard"),
    "openvpnconnect": simple("openvpn"),
    "powershell": favicon("learn.microsoft.com"),
    "onedrive": favicon("onedrive.live.com"),
    "winutil-onedrive": favicon("onedrive.live.com"),
    "claudedesktop": simple("anthropic"),
    "claudecode": simple("anthropic"),
    "chatgptdesktop": simple("openai"),
    "chatgpt": simple("openai"),
    "balenaetcher": favicon("etcher.balena.io"),
    "skype": favicon("www.skype.com"),
    "ciscowebex": simple("webex"),
    "webex": simple("webex"),
    "helium": favicon("helium.computer"),
    "dorion": favicon("github.com"),
    "winutil-dorion": favicon("github.com"),
    "codex": simple("openai"),
    "amazoncorretto8lts": simple("amazon"),
    "amazoncorretto21lts": simple("amazon"),
    "amazoncorretto25lts": simple("amazon"),
    "winutil-java8": simple("amazon"),
    "winutil-java21": simple("amazon"),
    "winutil-java25": simple("amazon"),
    "winutil-posh": favicon("ohmyposh.dev"),
    "winutil-wingetui": favicon("github.com"),
    "shutterencoder": favicon("www.shutterencoder.com"),
    "fusioncadnotes": favicon("www.autodesk.com"),
    "unigetui": favicon("github.com"),
    "naps2": favicon("www.naps2.com"),
    "winutil-naps2": favicon("www.naps2.com"),
    "pdfsam": favicon("pdfsam.org"),
    "winutil-pdfsam": favicon("pdfsam.org"),
    "notepadplus": "com.notepadqq.Notepadqq",
    "winutil-notepadplus": "com.notepadqq.Notepadqq",
    "deskflow": "org.deskflow.deskflow",
    "winutil-deskflow": "org.deskflow.deskflow",
}


def url_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"user-agent": UA["user-agent"]})
        with urllib.request.urlopen(req, timeout=10, context=CTX) as r:
            return 200 <= r.status < 400
    except Exception:
        # Some CDNs dislike HEAD — try a tiny GET
        try:
            req = urllib.request.Request(url, headers={"user-agent": UA["user-agent"]})
            with urllib.request.urlopen(req, timeout=10, context=CTX) as r:
                r.read(32)
                return True
        except Exception:
            return False


def resolve_curated(val: str) -> str | None:
    if val.startswith("http"):
        return val if url_ok(val) else None
    for url in (flathub(val), f"https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{val}.png"):
        if url_ok(url):
            return url
    return None


def search(q: str) -> list[dict]:
    req = urllib.request.Request(
        "https://flathub.org/api/v2/search",
        data=json.dumps({"query": q, "locale": "en"}).encode(),
        headers=UA,
    )
    with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
        hits = json.load(r)
    return hits if isinstance(hits, list) else hits.get("hits", [])


def load_apps() -> list[dict]:
    apps: list[dict] = []
    seen: set[str] = set()
    for path in sorted(CATALOG.glob("*.json")):
        if path.name == "icon-map.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for cat in data.get("categories", []):
            for app in cat.get("apps", []):
                if app.get("store") == "windows":
                    continue
                aid = app.get("id") or ""
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                apps.append(app)
    return apps


def main() -> None:
    # Prefer Warp's real Flathub id if present
    for key in ("warp", "warpterminal"):
        if url_ok(flathub("dev.warp.Warp")):
            CURATED[key] = "dev.warp.Warp"
        elif url_ok(flathub("app.drey.Warp")):
            CURATED[key] = "app.drey.Warp"

    # Cursor: use site favicon (simple-icons has no cursor slug)
    if url_ok(favicon("cursor.com")):
        CURATED["cursor"] = favicon("cursor.com")

    apps = load_apps()
    mapping: dict[str, dict] = {}

    for app in apps:
        if app.get("method") == "flatpak" and app.get("package"):
            mapping[app["id"]] = {
                "icon": flathub(app["package"]),
                "icon_id": app["package"],
                "source": "flatpak",
            }

    for app in apps:
        if app["id"] in mapping:
            continue
        for key in (app["id"], norm(app.get("name") or ""), norm(app.get("package") or "")):
            if key not in CURATED:
                continue
            url = resolve_curated(CURATED[key])
            if url:
                mapping[app["id"]] = {"icon": url, "source": "curated"}
                break

    remain = [a for a in apps if a["id"] not in mapping]
    for i, app in enumerate(remain):
        name = app.get("name") or ""
        n = norm(name)
        try:
            hits = search(name)
        except (urllib.error.URLError, TimeoutError) as exc:
            print("search fail", name, exc)
            continue
        for hit in hits:
            app_id = hit.get("app_id") or hit.get("id") or ""
            if app_id and norm(hit.get("name") or "") == n:
                mapping[app["id"]] = {
                    "icon": flathub(app_id),
                    "icon_id": app_id,
                    "source": "flathub-exact",
                }
                print("exact", name, "->", app_id)
                break
        if i % 8 == 7:
            time.sleep(0.1)

    OUT.write_text(json.dumps({"version": 1, "icons": mapping}, indent=2) + "\n", encoding="utf-8")
    miss = [a["name"] for a in apps if a["id"] not in mapping]
    print(f"wrote {OUT}  mapped {len(mapping)}/{len(apps)}  miss {len(miss)}")
    if miss:
        print("missing:", ", ".join(miss))
    print("refresh bundled logos: python3 scripts/vendor-app-icons.py")


if __name__ == "__main__":
    main()
