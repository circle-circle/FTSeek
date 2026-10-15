---
name: six-surface-zero-ref
description: Run FTSeek-US's repository-specific six-surface zero-reference audit before archiving, deleting, retiring, disabling, or otherwise removing a code, script, config, hook, cron, or prompt-field target. Do not use for ordinary code search or unrelated cleanup.
---

# FTSeek-US six-surface zero-reference audit

## Principle

Before changing the target, prove that no active consumer remains. A search limited to Python
import statements is insufficient: FTSeek-US also dispatches and loads code by strings and paths.

- Do not archive, delete, disable, or comment out the target while the audit is incomplete.
- Treat each surface separately; never collapse them into "already checked".
- A hit is a stop condition. Report it and ask for a routing decision instead of deleting around it.
- Unreadable or unavailable external state is unresolved, not zero.
- Use the bare target name, module path, file stem, route/key name, and path where they differ.

## Six Surfaces

1. **Repository references**: search the whole repository for the exact literals, including
   imports, string dispatch, `getattr`, YAML paths, tests, shell scripts, docs, and `archive/`.
   Classify every hit as an active consumer, target definition, stale documentation, historical
   record, or false positive; only active consumers block removal.
2. **Crontab**: inspect `crontab -l`, `/etc/crontab`, and `/etc/cron.d/*`; also check the
   crontab baseline under `docs/references/` when present.
3. **Systemd**: inspect `systemctl cat ftseek-us` and search `/etc/systemd/system` for the
   literal and for the service's working-directory-relative paths.
4. **Hermes prompt**: search `/root/.hermes/cron/jobs.json` for the literal and path forms.
5. **Git hooks**: search active files in `.git/hooks` (not only samples) for the literal and path.
6. **Scheduler/dynamic string surface**: search all string-bearing files, then specifically
   trace `_load_script_module`, `importlib`, `import_module`, and `getattr` dispatch sites. A
   file loaded by path or a route selected by string can have no import statement and still run.

For YAML-key removal, explicitly search `app/config_mgmt/validator.py`
(`REQUIRED_KEYS`), `app/core/config_audit.py` (`_AUDIT_ITEMS`), and
`app/core/config_defaults.py` (`CRITICAL_KEYS`) in addition to the six surfaces.

## Evidence Contract

Before removal, record a six-row result in the applicable DEB, RFC, or handoff entry:

| Surface | Exact command(s) | Hits | Classification / access result |

- Report literal search terms and external paths actually inspected.
- An unavailable crontab, systemd, Hermes, or hook source is unresolved; do not claim zero.
- Include target-definition exclusions so the count is reproducible.
- For zero-hit searches, state the scope; an empty result outside the real scope is not evidence.

If all six surfaces have zero active consumers and the user has authorized removal, perform the
removal and keep the evidence with the change. If any surface has an active or unresolved hit,
stop before mutation and report the exact consumer(s).

## Authority

`AGENT.md` is the authority for the original five surfaces. DEB-20260910 §8.6 extends the
repository-reference surface with the scheduler/dynamic string surface; this skill implements the
resulting six-surface standard and does not create a parallel rule. Read-only audit commands do
not restart or otherwise change service behavior.
