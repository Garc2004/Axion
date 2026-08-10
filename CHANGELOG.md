# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0] — unreleased

### Breaking

- **wg-easy upgraded from v14 to v15.** v15 is a ground-up rewrite that shares
  nothing with v14: not its configuration variables, not its API, not its data
  volume format. `WG_HOST`/`PASSWORD_HASH` are gone, replaced by `INIT_*`
  variables carrying the password **in the clear** (v15 hashes it itself), a
  **username** it now requires, and `INSECURE=true` without which the panel
  refuses to serve over HTTP. `INIT_*` applies **only on first boot**, exactly
  like `POSTGRES_PASSWORD`.

  **An existing v14 deployment needs a manual migration**: export `wg0.json`
  from the v14 panel and upload it in v15's setup wizard. `install` detects a
  v14 deployment and stops rather than letting v15 start on a volume it cannot
  read — which does not fail, it silently drops every enrolled client. See the
  README's "Upgrading to wg-easy v15".

- **The panel password now has a 12-character minimum.** Not our policy: it is
  what wg-easy's own login validation enforces. A shorter one creates the
  account and then makes every login fail with a 400 that looks like a wrong
  password.

- **`wireguard_admin_password_hash` is no longer accepted in `axion.toml`.**
  Use `wireguard_admin_password` with the real password. The unattended path
  reports this specifically rather than as a generic "missing password".

- **The whole project is now in English** — CLI output, prompts, `--help`,
  error panels, the comments in the generated `.env`/`wg.env`/
  `docker-compose.yml`, and the documentation. Anything scripted against the
  wizard's output strings will need updating.

### Added

- `docs/troubleshooting.md`: the failure modes that report no error of their
  own — containers vanishing, Smart App Control blocking the unsigned `.exe`,
  the empty panel after a wg-easy upgrade, host IP forwarding, the F5 symptom.
- `CONTRIBUTING.md` and an architecture overview in the README.
- The wizard reads the panel credentials from `wg.env` instead of asking for
  them again. Under v14 only the bcrypt hash was stored, so step 8 and
  `wireguard add-client` had to ask for the password a second time in the same
  run; v15 keeps it in the clear, so neither does now.

### Changed

- **Container images.** postgres 15.13 → 15.18-alpine, nginx 1.27 →
  1.31-alpine, ollama 0.6.5 → 0.32.6 (and `0.32.6-rocm`). Mattermost stays on
  10.5.1 and PostgreSQL on the 15 line deliberately: a major bump on either
  triggers migrations that do not come back, which is not something an
  installer should apply on its own.
- **Architecture.** `steps/runner.py` (805 lines, eighteen entry points, only
  one of them about steps) is split into a `commands/` package by what the user
  is trying to do. The loose root modules are grouped into `domain/` (what the
  stack is) and `render/` (how it looks). `steps/` now holds the install flow
  and nothing else.
- Service names live in one place (`domain/stack.py`) instead of being defined
  twice each across step modules.
- Deployment discovery moved out of step 9 into `domain/deployment.py`, where
  `doctor`, `wireguard add-client` and step 3's `restore()` can reach it
  without importing private names across module boundaries.
- One `.env` reader (`domain/deployment.env_value`) instead of four.
- The TUI's closing panel is now the CLI's, rendered through the shared
  `render_closing_summary`, rather than a hand-made copy that had drifted.
- The version is defined once, in `axion_wizard.__version__`, and read from
  there by the build.

### Removed

- `bcrypt` as a dependency, along with `hash_password`/`verify_password` and
  the `$$`-escaping of the bcrypt hash in `wg.env`. v15 takes the password in
  the clear. The ban on `$`, backtick and `!` stays: Compose still interpolates
  the values of `env_file:`, so now it is the password itself being protected.
- `CONTEXTO.md` from version control. It was a personal handover document
  carrying one machine's LAN address, hardware, local paths and a dated work
  log. Its durable content moved into `docs/troubleshooting.md` and the README.

### Fixed

- Writing a Rich renderable into the TUI's log raised `MissingStyle` as soon as
  it used an `axion.*` style, because Textual draws its `RichLog` with a
  `Console` that does not know the theme. `render.console.render_to_ansi`
  resolves the theme where it is defined.

## [0.2.2] — 2026-08-10

### Added

- Choose whether the AI's reply hangs off the message that triggered it (in a
  thread) or is posted as a normal channel message — `AI_REPLY_IN_THREAD`,
  asked for alongside the bot token.

### Fixed

- WireGuard client enrolment always failed: wg-easy v14 never returns the
  created client's id, so the id has to be deduced by listing before and after.

### Documentation

- A newly created Mattermost bot belongs to no team, and adding it straight to
  a channel fails with a message that does not say so.

## [0.2.0] — 2026-08-10

### Added

- A new install step for the Mattermost bot and webhook. Neither can be created
  without going through Mattermost's web interface, so the step stops, explains
  the two exact actions, and stores the resulting tokens — rather than leaving
  it to two commands that have to be remembered afterwards.

## [0.1.x] — 2026-08-10

### Fixed

- Compose project collisions between separate installs. Every install shared
  the project name `axion`, and Compose identifies a project by name rather
  than by directory, so a second install reused the first one's containers and
  volumes. `COMPOSE_PROJECT_NAME` now carries a random suffix and lives in
  `.env`.
- LF line endings pinned for shell scripts via `.gitattributes`: with CRLF the
  shebang is broken and Linux reports `env: bash\r: No such file or directory`.

## [0.1.0] — 2026-08-10

First release. The ten-step install flow with persisted progress, the
`doctor`/`models`/`model`/`wireguard` subcommands, the Textual alternative
interface, and the PyInstaller binary.

---

## Roadmap

Carried over from the project's handover notes, in rough order of usefulness:

- **Monitoring.** Dozzle (logs) and Uptime Kuma (alerts) are the obvious next
  additions. **Not** Watchtower — it conflicts with the ban on `:latest` — and
  not Prometheus/Grafana, which is disproportionate for a LAN.
- **Get the backups off the same disk.** They restore correctly, but they live
  next to what they protect: one disk failure takes both.
- **Sign the `.exe`**, if Smart App Control keeps blocking it (see
  `docs/troubleshooting.md`).
