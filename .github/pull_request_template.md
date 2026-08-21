## Summary

<!-- What changed and why. -->

## Test plan

- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `shellcheck -S warning -x lib/core/*.sh lib/plugins/*.sh bin/urstack install.sh` (if shell changed)
- [ ] Manual check: <!-- GUI page, CLI flag, or Fedora spin -->

## Privileged / PolicyKit

- [ ] No new pkexec surface, **or** new work is an explicit `priv.sh` job with validated inputs
- [ ] Policy does not use `auth_admin_keep`
