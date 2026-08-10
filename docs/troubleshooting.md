# Troubleshooting

Failures that are hard to diagnose from the outside, because the thing that
went wrong does not report an error — it just quietly does nothing.

## The stack is empty and the containers are gone

Containers can disappear without any action that explains it. This has been
observed once with Docker Desktop and WSL still running and every volume
intact.

**Before reinstalling anything**, check that the volumes are still there and
try to bring the stack back up:

```bash
docker volume ls | grep axion
axion-wizard up
```

Everything was recovered this way with no data loss. The expensive mistake
here is treating a stopped stack as a lost one.

## Windows blocks the binary: "An app control policy blocked this file"

Smart App Control rejects executables with no signature and no reputation.
Check whether it is on:

```powershell
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' `
  -Name VerifiedAndReputablePolicyState
```

**It is not deterministic.** It has blocked one build and let the next one
through with no code change in between — the verdict tracks the hash of the
specific file, so a clean rebuild usually clears it:

```powershell
Remove-Item dist\axion-wizard.exe, build\work -Recurse -Force
.\build\build.ps1
```

If it recurs and rebuilding does not help, in order of sanity:

1. Use the Linux binary inside WSL, where Smart App Control does not apply.
2. Run from source, which is never affected:
   `.venv\Scripts\python.exe -m axion_wizard --project-dir <dir> doctor`
3. Sign the `.exe` with a code-signing certificate.
4. Turn Smart App Control off. **This is irreversible** — re-enabling it
   requires reinstalling Windows. Not recommended.

## The WireGuard panel is empty after upgrading

wg-easy v15 cannot read a v14 data volume. If it starts on one it does not
fail: it finds a store it does not recognise, launches its setup wizard, and
leaves you with an empty panel — every already-enrolled client stops
connecting at once, and nothing in the logs mentions that there was data to
migrate.

`axion-wizard install` refuses to proceed when it detects this, and prints
the migration steps. To do it by hand:

1. Open the **v14** panel and use its backup button to download `wg0.json`.
2. Run `axion-wizard install`. The v15 panel comes up in its setup wizard.
3. Choose "I already have a configuration file" and upload `wg0.json`.

If there are no clients worth keeping, `axion-wizard uninstall --purge`
removes the volume and lets the next install start clean.

## The AI only answers when the page is reloaded

The outgoing webhook fires, but Mattermost gives up on the request after
about 30 seconds and a slow model's answer is lost whole, with no error
anywhere.

The fix is a bot token, which switches the FastAPI bridge to asynchronous
mode: it answers the webhook immediately and posts the reply through the API
once the model finishes.

```bash
axion-wizard set-bot-token <token>
```

Create the bot in Mattermost under *Integrations → Bot Accounts*. **A newly
created bot belongs to no team**: add it to the team and to every channel it
should answer in, or Mattermost rejects the post.

## The WireGuard tunnel connects but no traffic arrives

On native Linux with `network_mode: host`, the host's IP forwarding has to be
on. With it off, wg-easy starts without complaining, the tunnel establishes
and the handshake succeeds — but no packet reaches its destination, and
nothing is written to any log.

`axion-wizard install` enables it and warns if it could not. To check:

```bash
sysctl net.ipv4.ip_forward     # must be 1
```

## Mattermost shows fewer settings than `printenv`

Mattermost's environment variables do not rewrite its `config.json` — they
override it in memory. Seeing 30 settings in the file and 180 in `printenv`
is normal, not a fault.

## `install` seems to apply nothing

Progress is persisted per step, so a second `install` skips what is already
done. After a wizard upgrade that changed the templates, that means the new
templates may not be written at all.

```bash
axion-wizard install --restart    # ignore saved progress, redo from step 1
```

## Things about this project that are easy to miss

- **`MANAGED_SERVICES`** is regenerated on every `install`; anything not on
  that list is preserved. Hand-editing a managed service achieves nothing.
- **`PRESERVED_ENV_KEYS`** carries the webhook token, bot token, system
  prompt and the two backup settings across installs. The PostgreSQL password
  is preserved by a different route, in step 3.
- **No `$`, backtick or `!`** in values headed for a `.env`. Compose
  interpolates the values of `env_file:`, so an unescaped `$` is read as a
  variable and silently eaten.
- **Images always carry a pinned tag.** `latest` for wg-easy means whichever
  major is current, and each major configures itself incompatibly with the
  last — without a single error in the logs.
- **Verification always re-reads from the other side** rather than trusting
  what was just written. The certificate's SAN is read back from the file;
  the panel password is read back from the running container.
