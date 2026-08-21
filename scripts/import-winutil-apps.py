#!/usr/bin/env python3
"""Import Chris Titus Tech winutil applications into UrStack catalog format.

Source: https://github.com/ChrisTitusTech/winutil (config/applications.json)

Only titles with a Linux install method in LINUX_MAP are kept. Windows-only
WinUtil apps are dropped.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

WINUTIL_URL = (
    "https://raw.githubusercontent.com/ChrisTitusTech/winutil/main/config/applications.json"
)

# winutil category → UrStack category id/name
CAT_MAP = {
    "Browsers": ("browsers", "Browsers"),
    "Communications": ("communication", "Communication"),
    "Development": ("developer", "Developer"),
    "Document": ("productivity", "Productivity"),
    "Games": ("gaming", "Gaming"),
    "Microsoft Tools": ("microsoft", "Microsoft tools"),
    "Multimedia Tools": ("media", "Media"),
    "Pro Tools": ("pro-tools", "Pro tools"),
    "Selfhosted Tools": ("selfhosted", "Self-hosted"),
    "Utilities": ("utilities", "Utilities"),
}

# Command-line / toolchain keys — not GUI apps. Shown under CLIs & tools.
CLI_KEYS = {
    "cmake",
    "claude-code",
    "codex",
    "fnm",
    "git",
    "githubcli",
    "golang",
    "java8",
    "java21",
    "java25",
    "lazygit",
    "neovim",
    "nodejs",
    "nodejslts",
    "pnpm",
    "posh",
    "python3",
    "rustlang",
    "starship",
    "vagrant",
    "yarn",
    "uv",
    "Ruby",
    "Lua",
}

# winutil key → (method, package[, url_override])
# Prefer Flatpak / DNF / known vendor installers on Fedora.
LINUX_MAP: dict[str, tuple] = {
    # Browsers
    "brave": ("flatpak", "com.brave.Browser"),
    "chrome": ("dnf", "google-chrome-stable"),
    "chromium": ("flatpak", "org.chromium.Chromium", "https://www.chromium.org/"),
    "edge": ("browser", "microsoft-edge", "https://www.microsoft.com/edge/download"),
    "firefox": ("flatpak", "org.mozilla.firefox"),
    "firefoxesr": ("flatpak", "org.mozilla.firefox"),
    "floorp": ("browser", "floorp", "https://floorp.app/en/download"),
    "librewolf": ("flatpak", "io.gitlab.librewolf-community"),
    "mullvadbrowser": ("browser", "mullvad-browser", "https://mullvad.net/en/download/browser"),
    "tor": ("browser", "tor-browser", "https://www.torproject.org/download/"),
    "ungoogled": ("flatpak", "io.github.ungoogled_software.ungoogled_chromium"),
    "vivaldi": ("flatpak", "com.vivaldi.Vivaldi"),
    "waterfox": ("browser", "waterfox", "https://www.waterfox.net/download/"),
    "ZenBrowser": ("flatpak", "app.zen_browser.zen"),
    "helium": ("browser", "helium", "https://github.com/imputnet/helium/"),
    # Communication
    "discord": ("flatpak", "com.discordapp.Discord"),
    "signal": ("flatpak", "org.signal.Signal"),
    "slack": ("flatpak", "com.slack.Slack"),
    "telegram": ("flatpak", "org.telegram.desktop"),
    "matrix": ("flatpak", "im.riot.Riot"),
    "zoom": ("flatpak", "us.zoom.Zoom"),
    "teams": ("browser", "teams", "https://www.microsoft.com/microsoft-teams/download-app"),
    "thunderbird": ("flatpak", "org.mozilla.Thunderbird"),
    "betterbird": ("flatpak", "eu.betterbird.Betterbird"),
    "vesktop": ("flatpak", "dev.vencord.Vesktop"),
    "viber": ("browser", "viber", "https://www.viber.com/en/download/"),
    "whatsapp": ("flatpak", "com.rtosta.zapzap"),
    "protonmail": ("flatpak", "me.proton.Mail"),
    "chatterino": ("flatpak", "com.chatterino.chatterino"),
    "qtox": ("flatpak", "io.github.qtox.qTox"),
    "teamspeak3": ("browser", "teamspeak3", "https://www.teamspeak.com/en/downloads/"),
    "teamspeak6": ("browser", "teamspeak6", "https://www.teamspeak.com/en/downloads/"),
    "dorion": ("browser", "dorion", "https://github.com/SpikeHD/Dorion"),
    # Developer
    "bruno": ("flatpak", "com.usebruno.Bruno"),
    "cursor": ("cursor_rpm", "cursor"),
    "dockerdesktop": ("browser", "docker-desktop", "https://docs.docker.com/desktop/setup/install/linux/"),
    "git": ("dnf", "git"),
    "githubcli": ("dnf", "gh"),
    "githubdesktop": ("flatpak", "io.github.shiftey.Desktop"),
    "golang": ("dnf", "golang"),
    "jetbrains": ("toolbox_tarball", "jetbrains-toolbox"),
    # Not in the Fedora repos (COPR only) — link the upstream instructions
    # rather than offer a `dnf install` that always fails.
    "lazygit": ("browser", "lazygit", "https://github.com/jesseduffield/lazygit#installation"),
    "neovim": ("dnf", "neovim"),
    "nodejs": ("dnf", "nodejs"),
    "nodejslts": ("dnf", "nodejs"),
    "postman": ("flatpak", "com.getpostman.Postman"),
    "python3": ("dnf", "python3"),
    "rustlang": ("script", "rustup", "https://sh.rustup.rs"),
    "starship": ("browser", "starship", "https://starship.rs/guide/"),
    "sublimetext": ("browser", "sublime-text", "https://www.sublimetext.com/download"),
    "vscode": ("flatpak", "com.visualstudio.code"),
    "vscodium": ("flatpak", "com.vscodium.codium"),
    "Zed": ("flatpak", "dev.zed.Zed"),
    "cmake": ("dnf", "cmake"),
    "yarn": ("dnf", "yarnpkg"),
    "pnpm": ("script", "pnpm", "https://get.pnpm.io/install.sh"),
    "uv": ("script", "uv", "https://astral.sh/uv/install.sh"),
    "fnm": ("script", "fnm", "https://fnm.vercel.app/install"),
    "vagrant": ("dnf", "vagrant"),
    "unity": ("browser", "unityhub", "https://unity.com/download"),
    "claude-code": ("browser", "claude", "https://code.claude.com/"),
    "codex": ("browser", "codex", "https://developers.openai.com/codex/cli"),
    # Fedora 44 ships only java-latest-openjdk (25); the 8 and 21 packages were
    # retired, so point those at Adoptium builds instead of a failing install.
    "java8": ("browser", "temurin-8", "https://adoptium.net/temurin/releases/?version=8"),
    "java21": ("browser", "temurin-21", "https://adoptium.net/temurin/releases/?version=21"),
    "java25": ("dnf", "java-latest-openjdk"),
    "Ruby": ("dnf", "ruby", "https://www.ruby-lang.org/"),
    "Lua": ("dnf", "lua", "https://www.lua.org/"),
    "posh": ("browser", "oh-my-posh", "https://ohmyposh.dev/docs/installation/linux"),
    # Document / productivity
    "joplin": ("flatpak", "net.cozic.joplin_desktop"),
    "libreoffice": ("flatpak", "org.libreoffice.LibreOffice"),
    "onlyoffice": ("flatpak", "org.onlyoffice.desktopeditors"),
    "obsidian": ("flatpak", "md.obsidian.Obsidian"),
    "okular": ("flatpak", "org.kde.okular"),
    "zotero": ("flatpak", "org.zotero.Zotero"),
    "xournal": ("flatpak", "com.github.xournalpp.xournalpp"),
    "pdfsam": ("flatpak", "org.pdfsam.PDFSam"),
    "simplenote": ("flatpak", "com.simplenote.Simplenote"),
    "naps2": ("flatpak", "com.naps2.Naps2"),
    # Games
    "steam": ("flatpak", "com.valvesoftware.Steam"),
    "heroiclauncher": ("flatpak", "com.heroicgameslauncher.hgl"),
    "prismlauncher": ("flatpak", "org.prismlauncher.PrismLauncher"),
    "itch": ("browser", "itch", "https://itch.io/app"),
    "geforcenow": ("browser", "geforcenow", "https://www.nvidia.com/en-us/geforce-now/"),
    "roblox": ("browser", "sober", "https://sober.vinegarhq.org/"),
    "cemu": ("flatpak", "info.cemu.Cemu"),
    "modrinth": ("flatpak", "com.modrinth.ModrinthApp"),
    # Media
    "audacity": ("flatpak", "org.audacityteam.Audacity"),
    "blender": ("flatpak", "org.blender.Blender"),
    "calibre": ("flatpak", "com.calibre_ebook.calibre"),
    "gimp": ("flatpak", "org.gimp.GIMP"),
    "handbrake": ("flatpak", "fr.handbrake.ghb"),
    "obs": ("flatpak", "com.obsproject.Studio"),
    "vlc": ("flatpak", "org.videolan.VLC"),
    "mpv": ("flatpak", "io.mpv.Mpv"),
    "mpc-qt": ("flatpak", "io.github.mpc_qt.mpc-qt"),
    "nomacs": ("flatpak", "org.nomacs.ImageLounge"),
    "notepadplus": ("flatpak", "com.notepadqq.Notepadqq"),  # Linux alternative
    # Pro tools
    "nmap": ("dnf", "nmap"),
    "wireshark": ("dnf", "wireshark"),
    "wireguard": ("dnf", "wireguard-tools"),
    "putty": ("dnf", "putty"),
    "mullvadvpn": ("browser", "mullvad-vpn", "https://mullvad.net/en/download/vpn/linux"),
    "protonvpn": ("flatpak", "com.protonvpn.www"),
    "OpenVPN": ("dnf", "openvpn"),
    "ventoy": ("browser", "ventoy", "https://www.ventoy.net/en/download.html"),
    "angryipscanner": ("flatpak", "org.angryip.ipscan"),
    "cinebenchr23": ("browser", "cinebench", "https://www.maxon.net/en/downloads/cinebench-2024-downloads"),
    # Self-hosted
    "jellyfinmediaplayer": ("flatpak", "com.github.iwalton3.jellyfin-media-player"),
    "jellyfinserver": ("browser", "jellyfin", "https://jellyfin.org/downloads/"),
    "kodi": ("flatpak", "tv.kodi.Kodi"),
    "localsend": ("flatpak", "org.localsend.localsend_app"),
    "moonlight": ("flatpak", "com.moonlight_stream.Moonlight"),
    "nextclouddesktop": ("flatpak", "com.nextcloud.desktopclient.nextcloud"),
    "plexdesktop": ("browser", "plex-desktop", "https://www.plex.tv/media-server-downloads/"),
    "plex": ("browser", "plex-media-server", "https://www.plex.tv/media-server-downloads/"),
    "sunshine": ("flatpak", "dev.lizardbyte.app.Sunshine"),
    "netbird": ("browser", "netbird", "https://netbird.io/"),
    # Utilities
    "1password": ("browser", "1password", "https://1password.com/downloads/linux/"),
    "anydesk": ("browser", "anydesk", "https://anydesk.com/en/downloads/linux"),
    "bitwarden": ("flatpak", "com.bitwarden.desktop"),
    "keepassxc": ("flatpak", "org.keepassxc.KeePassXC"),
    "dropbox": ("browser", "dropbox", "https://www.dropbox.com/install-linux"),
    "teamviewer": ("browser", "teamviewer", "https://www.teamviewer.com/en/download/linux/"),
    "qbittorrent": ("flatpak", "org.qbittorrent.qBittorrent"),
    "OVirtualBox": ("browser", "VirtualBox", "https://www.virtualbox.org/wiki/Linux_Downloads"),
    "peazip": ("flatpak", "io.github.peazip.PeaZip"),
    # Fedora renamed this to 7zip; p7zip only survives as a virtual provide, so
    # using it here installs fine but never matches an installed-package check.
    "7zip": ("dnf", "7zip"),
    "parsec": ("browser", "parsec", "https://parsec.app/downloads"),
    "openrgb": ("flatpak", "org.openrgb.OpenRGB"),
    "protonpass": ("flatpak", "me.proton.Pass"),
    "protondrive": ("browser", "proton-drive", "https://proton.me/drive/download"),
    "protonauth": ("browser", "proton-authenticator", "https://proton.me/authenticator"),
    "enteauth": ("flatpak", "io.ente.auth"),
    "hugo": ("dnf", "hugo"),
    "tightvnc": ("dnf", "tigervnc", "https://tigervnc.org/"),
    "deskflow": ("flatpak", "org.deskflow.deskflow"),
    "powershell": ("browser", "powershell", "https://learn.microsoft.com/powershell/scripting/install/install-rhel"),
}

# WinUtil keys with no Linux desktop (or only a Wine/Proton story). Dropped
# unless LINUX_MAP remaps them to Flatpak/DNF/script.
WINDOWS_ONLY = {
    "autoruns", "processexplorer", "processmonitor", "tcpview", "rdcman",
    "vc2015_32", "vc2015_64", "powertoys", "terminal", "ntlite", "dismtools",
    "startallback", "ttaskbar", "msedgeredirect", "nvclean", "sdio",
    "bulkcrapuninstaller", "revo", "WiseProgramUninstaller", "winrar",
    "totalcommander", "treesize", "wiztree", "files", "glazewm", "nilesoftShell",
    "OFGB", "OPAutoClicker", "blurautoclicker", "policyplus", "processlasso",
    "msiafterburner", "signalrgb", "minitoolpartitionwizard", "nanazip",
    "xeheditor", "jpegview", "internetdownloadmanager", "crystaldiskinfo",
    "crystaldiskmark", "eartrumpet", "sharex", "Paintdotnet", "irfanview",
    "imageglass", "klite", "mpchc", "notepadplus", "aimp", "foobar",
    "winscp", "putty", "gsudo", "simplewall", "cpuz", "gpuz", "hwinfo",
    "hwmonitor", "ddu", "advancedip", "sumatra", "pdf-xchange", "pdf24creator",
    "pdfgear", "foxpdfreader", "adobe", "gitextensions", "systeminformer",
    "visualstudio2022", "visualstudio2026", "dotnet6", "dotnet8",
    "dotnet9", "dotnet10", "playnite", "Overwolf", "vrdesktopstreamer",
    "ubisoft", "gog", "epicgames", "eaapp", "rufus", "everything", "autohotkey",
    "flux", "itunes", "wingetui", "chatgpt", "claude", "nuget", "onedrive",
    "googledrive",
}


def fetch_winutil() -> dict:
    with urllib.request.urlopen(WINUTIL_URL, timeout=60) as resp:
        return json.load(resp)


def map_app(key: str, entry: dict) -> dict:
    name = entry.get("content") or key
    summary = entry.get("description") or name
    link = entry.get("link") or ""
    method, package, url = "browser", key, link

    if key in LINUX_MAP:
        mapped = LINUX_MAP[key]
        method = mapped[0]
        package = mapped[1]
        if len(mapped) > 2:
            url = mapped[2]
        elif method == "browser":
            url = link
    else:
        url = link

    store = "vendor"
    if method == "flatpak":
        store = "flathub"
    elif method == "dnf":
        store = "fedora"

    out = {
        "id": f"winutil-{key}",
        "name": name,
        "summary": summary[:180],
        "method": method,
        "package": package,
        "store": store,
        "source": "winutil",
        "winutil_id": key,
    }
    if url:
        out["url"] = url
    if method in {"cursor_rpm", "toolbox_tarball", "script", "browser", "dnf"}:
        out["detect"] = package
    return out


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out_path = root / "data" / "catalog" / "winutil.json"
    data = fetch_winutil()

    buckets: dict[str, dict] = {}
    for key, entry in data.items():
        # UrStack is Fedora/Linux: skip unmapped WinUtil titles and Windows-only
        # tools unless LINUX_MAP remaps them to a real Linux install method.
        if key not in LINUX_MAP:
            continue
        mapped = LINUX_MAP[key]
        if key in WINDOWS_ONLY and mapped[0] == "browser":
            continue
        wcat = entry.get("category", "Utilities")
        cid, cname = CAT_MAP.get(wcat, ("utilities", "Utilities"))
        if key in CLI_KEYS:
            cid, cname = "cli", "CLIs & tools"
        # Merge into the same category ids/names as curated apps.json
        if cid not in buckets:
            buckets[cid] = {"id": cid, "name": cname, "apps": []}
        buckets[cid]["apps"].append(map_app(key, entry))

    for cid, bucket in list(buckets.items()):
        if not bucket["apps"]:
            del buckets[cid]

    # Stable category order matching UrStack + winutil extras
    order = [
        "browsers",
        "communication",
        "developer",
        "cli",
        "productivity",
        "gaming",
        "microsoft",
        "media",
        "pro-tools",
        "selfhosted",
        "utilities",
    ]
    categories = [buckets[k] for k in order if k in buckets]
    for k, v in buckets.items():
        if k not in order:
            categories.append(v)

    payload = {
        "version": 1,
        "source": "https://github.com/ChrisTitusTech/winutil",
        "source_file": "config/applications.json",
        "note": "Imported from Chris Titus Tech winutil; only Linux install methods are kept.",
        "categories": categories,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    total = sum(len(c["apps"]) for c in categories)
    print(f"Wrote {out_path} ({total} apps, {len(categories)} categories)")


if __name__ == "__main__":
    main()
