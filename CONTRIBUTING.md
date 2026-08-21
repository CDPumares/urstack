# Contributing to UrStack

Thanks for helping. The most useful changes keep privileged work in `lib/core/priv.sh`, stay Fedora-workstation-shaped, and do not widen the pkexec attack surface.

Most of this repo was written with AI coding assistants; see [AI use](README.md#ai-use) in the README. Please still read and test the code you touch.

## Development setup

```bash
git clone https://github.com/CDPumares/urstack.git
cd urstack
./install.sh --user
```

You can also run from the clone without installing: `./bin/urstack` (GUI) or `./bin/urstack --check` (CLI).

User config lives at `~/.config/urstack/config.conf` and is never overwritten by the installer if it already exists.

## Checks to run

CI on `main` and pull requests runs the same gates.

```bash
# Shell syntax
bash -n bin/urstack install.sh
find . -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -n1 bash -n

# shellcheck: errors and warnings must be clean
shellcheck -S warning -x lib/core/*.sh lib/plugins/*.sh bin/urstack install.sh

# Python: blocking correctness subset
ruff check --isolated --select F,E9,B .

# Compile and unit tests (GTK / libadwaita tests skip if those GIRs are missing)
python3 -m compileall -q lib scripts tests
python3 -m unittest discover -s tests -v
```

PolicyKit XML under `data/polkit/` must parse, and must **not** use `auth_admin_keep`. The privileged helper takes a jobs file from an unprivileged caller, so every elevation has to re-prompt.

## Privileged code

- New root/sudo work belongs in `lib/core/priv.sh` as an explicit job verb, with the same input validation already used there.
- Do not pass unsanitized paths, package lists, or shell fragments from the UI into `pkexec`.
- Do not add `auth_admin_keep` (or equivalent cached authorization) to the policy.

## Catalog and icons

App entries live in `data/catalog/apps.json` (and `winutil.json` for the winutil-mapped set). GUI editors and IDEs go in `developer`; command-line tools, language toolchains, and other non-app utilities go in `cli`. Helpers:

```bash
python3 scripts/import-winutil-apps.py
python3 scripts/build-icon-map.py
python3 scripts/vendor-app-icons.py
python3 scripts/vendor-app-metadata.py
```

Prefer Flatpak, DNF, or Snap when that is the realistic Fedora path. Vendor / browser install is a last resort, and only when the vendor ships a Linux build. Do not add Windows-only apps (WinGet, iTunes, EA App, UniGetUI, and so on).

## Pull requests

- One concern per PR when you can.
- Include a short test plan (CLI flag, GUI page, or Fedora spin).
- Update `README.md` if you change user-facing flags, config keys, or install behaviour.

Open an issue first for large design changes (new package sources, restore behaviour, PolicyKit jobs).
