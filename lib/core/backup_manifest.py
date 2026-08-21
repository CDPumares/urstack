#!/usr/bin/env python3
"""Write and verify blueprint MANIFEST.sha256 without GNU sha256sum -c.

sha256sum -c treats `:` as the status delimiter, so Copr repo files named
`_copr:copr.…:nushell.repo` are reported as missing privileged files.
Kate tools named with spaces are often listed as `%20` after a copy, so the
real files look unlisted and the encoded names look missing.

FAT/exFAT/NTFS cannot store `:`. A USB copy of a Copr repo often leaves a
stub named `_copr` or `_copr:` and drops the rest. Those are copy artifacts,
not tampering. Backups now store `:` as `%3A`; verify accepts either form.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, unquote

MANIFEST_NAME = "MANIFEST.sha256"

_SKIP_NAMES = {
    MANIFEST_NAME,
    ".INCOMPLETE",
    "BACKUP_SUMMARY.txt",
    "BACKUP_MANIFEST.md",
    "RESTORE_REPORT.txt",
    "RESTORE_LOG.txt",
}

_GNU_LINE = re.compile(r"^\\?([0-9a-f]{64}) [ *](.*)$")
_HASH_ONLY = re.compile(r"^[0-9a-f]{64}$")


def is_privileged(rel: str) -> bool:
    p = rel[2:] if rel.startswith("./") else rel
    return p.startswith("manifests/") or p.startswith("config/etc/")


def _rel_key(rel: str) -> str:
    rel = rel.replace("\\", "/")
    if not rel.startswith("./"):
        rel = "./" + rel.lstrip("/")
    return rel


def _base(rel: str) -> str:
    return _rel_key(rel).rsplit("/", 1)[-1]


def path_aliases(rel: str) -> list[str]:
    """Same path as written, URL-decoded, or with spaces/colons encoded."""
    rel = _rel_key(rel)
    seen: list[str] = []

    def add(p: str) -> None:
        p = _rel_key(p)
        if p not in seen:
            seen.append(p)

    add(rel)
    add(unquote(rel))
    decoded = unquote(rel)
    add(decoded.replace(" ", "%20"))
    add(decoded.replace(":", "%3A"))
    add(decoded.replace(" ", "%20").replace(":", "%3A"))
    add(quote(decoded, safe="/._-"))
    return seen


def fat_truncation_aliases(rel: str) -> list[str]:
    """Stubs left when a filesystem splits `_copr:copr.…:pkg.repo` at the first `:`."""
    rel = _rel_key(rel)
    name = _base(rel)
    if ":" not in name and "%3A" not in name.upper():
        return []
    decoded = unquote(name).replace("%3A", ":").replace("%3a", ":")
    if ":" not in decoded:
        return []
    prefix = decoded.split(":", 1)[0]
    parent = rel[: -len(name)].rstrip("/") or "."
    return [f"{parent}/{prefix}", f"{parent}/{prefix}:"]


def name_is_fat_fragile(rel: str) -> bool:
    name = unquote(_base(rel))
    return ":" in name or "%3A" in _base(rel).upper()


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _try_digest(path: Path) -> str | None:
    try:
        if path.is_file() and not path.is_symlink():
            return _file_digest(path)
    except OSError:
        return None
    return None


def iter_blueprint_files(root: Path) -> list[str]:
    rels: list[str] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in {".git"}]
        for name in filenames:
            if name in _SKIP_NAMES and Path(dirpath) == root:
                continue
            path = Path(dirpath) / name
            try:
                if not path.is_file() or path.is_symlink():
                    continue
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            rels.append("./" + rel)
    rels.sort()
    return rels


def parse_manifest_line(line: str) -> tuple[str, str] | None:
    line = line.rstrip("\n")
    if not line or line.startswith("#"):
        return None
    m = _GNU_LINE.match(line)
    if not m:
        return None
    digest, name = m.group(1), m.group(2)
    if line.startswith("\\"):
        name = name.encode("utf-8").decode("unicode_escape")
    return digest, _rel_key(name)


def write_manifest(root: Path) -> Path:
    dest = root / MANIFEST_NAME
    lines = []
    for rel in iter_blueprint_files(root):
        digest = _file_digest(root / rel[2:])
        lines.append(f"{digest}  {rel}\n")
    dest.write_text("".join(lines), encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    return dest


def _open_alias(root: Path, rel: str, digest: str | None = None) -> tuple[Path, str] | None:
    for alias in path_aliases(rel):
        path = root / alias[2:]
        if path.is_file() and not path.is_symlink():
            return path, alias
    if digest:
        for leftover_rel in fat_truncation_aliases(rel):
            path = root / leftover_rel[2:]
            got = _try_digest(path)
            if got == digest:
                return path, leftover_rel
    return None


def materialize_fat_repo_names(root: Path) -> int:
    """If a USB stub's checksum matches a listed Copr file, write the `%3A` name.

    Restore can then install `*.repo` files. No-op when the volume is read-only.
    """
    man = root / MANIFEST_NAME
    if not man.is_file():
        return 0
    listed: dict[str, str] = {}
    for raw in man.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_manifest_line(raw)
        if parsed:
            digest, rel = parsed
            listed[rel] = digest
    n = 0
    for rel, digest in listed.items():
        if not name_is_fat_fragile(rel):
            continue
        if _open_alias(root, rel) is not None:
            continue
        found = _open_alias(root, rel, digest)
        if found is None:
            continue
        src, _alias = found
        dest = src.parent / _base(rel).replace(":", "%3A")
        if dest.resolve() == src.resolve():
            continue
        try:
            dest.write_bytes(src.read_bytes())
            n += 1
        except OSError:
            continue
    return n


def verify_manifest(root: Path) -> tuple[int, list[str]]:
    """Return (0 ok, 1 problems, 2 missing manifest) and report lines."""
    man = root / MANIFEST_NAME
    if not man.is_file():
        return 2, []
    listed: dict[str, str] = {}
    for raw in man.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_manifest_line(raw)
        if parsed:
            digest, rel = parsed
            listed[rel] = digest

    problems: list[str] = []
    accounted: set[str] = set()
    truncation_ok: set[str] = set()
    for rel in listed:
        accounted.update(path_aliases(rel))
        truncation_ok.update(fat_truncation_aliases(rel))

    for rel, digest in listed.items():
        found = _open_alias(root, rel, digest)
        if found is None:
            if name_is_fat_fragile(rel):
                # Copr names cannot survive FAT/exFAT. Not tampering.
                continue
            problems.append(f"{rel}: FAILED open or read")
            continue
        path, alias = found
        accounted.add(alias)
        accounted.update(path_aliases(rel))
        try:
            actual = _file_digest(path)
        except OSError:
            problems.append(f"{rel}: FAILED open or read")
            continue
        if actual != digest:
            problems.append(f"{rel}: FAILED")

    on_disk = iter_blueprint_files(root)
    for rel in on_disk:
        if rel in accounted or rel in truncation_ok:
            continue
        if any(a in listed for a in path_aliases(rel)):
            continue
        problems.append(f"{rel}: not listed in the manifest")
    return (1 if problems else 0), problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[0] not in {"write", "verify", "repair"}:
        print(
            "usage: backup_manifest.py write <dest> | verify <dest> <report> | repair <dest>",
            file=sys.stderr,
        )
        return 2
    cmd, dest_s = args[0], args[1]
    dest = Path(dest_s)
    if cmd == "write":
        write_manifest(dest)
        return 0
    if cmd == "repair":
        materialize_fat_repo_names(dest)
        return 0
    report_s = args[2] if len(args) > 2 else ""
    rc, problems = verify_manifest(dest)
    if report_s:
        Path(report_s).write_text("".join(p + "\n" for p in problems), encoding="utf-8")
    else:
        sys.stdout.write("".join(p + "\n" for p in problems))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
