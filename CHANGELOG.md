# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`install --tui` now picks the AI model from the same catalogue the CLI
  offers**, instead of a bare text box with a placeholder. The list is ranked
  by fit to the detected hardware, with the recommendation marked, the size,
  and whether a model is already installed or exceeds the machine — drawn by
  the same `describe_model` the questionary prompt uses, so the two cannot
  drift. It stays an editable field rather than a closed list, because §5
  requires the escape hatch: Ollama's library grows constantly and any
  catalogue falls short. The model already named in `.env` is pre-selected, so
  anyone who ran `model set` and then reinstalls does not lose that choice by
  pressing on. With no catalogue reachable (offline) the field degrades to
  plain free text rather than to an empty box.
- **`install --tui` shows the configuration summary and asks for confirmation
  before writing anything**, which the sequential flow has always done and this
  one never did — the interface that shows the most was the one that never
  showed what it was about to do. It is `s03_config.render_summary`, the same
  panel, so secrets come out masked here too. `--yes` skips it, as it already
  skipped the equivalent prompt on the CLI.

### Fixed

- **`doctor`'s results table printed two of its three headers in Spanish**
  (`Resultado`, `Detalle`) — left behind by the translation to English, on the
  surface the tool prints most often. The same table shape is now built once
  in `render.ui.make_check_table` and shared with `network-check`, which had
  the English headers: the drift that module exists to prevent had happened
  inside it.
- **`doctor` named its WebSocket row `WebSocket Mattermost`, but the README
  and step 1's own hint both send the user to "the `Mattermost WebSocket`
  row".** The one instruction given for telling the WSL2 mirrored-networking
  stall apart from a configuration problem pointed at a row that did not
  exist under that name. Renamed to match what the documentation promises.
- The rest of the Spanish left behind in `install --tui` after the English
  translation: the `Progreso`/`Registro` panel borders, the `Salir` binding,
  the `Instalando…` subtitle two methods away from a `Complete` in English,
  and `Detectando entorno…`. Also `verify`'s "N comprobaciones OK" and an
  untranslated WebSocket parse error.

## [0.3.4] — 2026-08-12

### Fixed

- **The first WireGuard client could never be created.** Reproduced against a
  real wg-easy v15.3.0: `POST /api/client`'s zod schema declares `expiresAt`
  `.nullable()` without `.optional()` — it accepts `null` or a string, but
  not the key being absent — so a request carrying only `{"name": ...}`
  always came back `400 "expiresAt is required"`, in both step 8 and
  `wireguard add-client`. The request now sends `expiresAt: null`; confirmed
  live, the same request that used to 400 now returns `200` with a
  `clientId`. Same trap already documented here for `login`'s `remember`.
- The failure message for that case (and any other client-creation error)
  only ever showed `AxionError.__str__`, which is just `what` — the actual
  cause (`why`, e.g. "expiresAt is required") never reached the screen. Both
  are shown now.
- Step 1 could print up to four independent warnings (GPU, IPv6, crossed
  filesystem, LAN exposure) back to back with no blank line between them,
  reading as one undifferentiated paragraph instead of four distinct notes —
  common on Docker Desktop for Windows, where several fire in the same run.
  Separated. One of the four (client isolation on the router, when mirrored
  networking already looks correctly configured) was being recorded for the
  closing summary but never actually printed live — the only one of six
  similar warnings in that file missing its `console.print`. It now shows up
  when it happens, not just at the very end.

### Changed

- The instructions printed after a client's QR (step 8 and `wireguard
  add-client`) go from one generic line — "scan the QR or import the
  configuration manually" — to the actual steps: where to get WireGuard,
  which menu item to use on mobile versus desktop, and to turn the tunnel on
  once imported.

## [0.3.3] — 2026-08-12

### Fixed

- **Step 9's Mattermost bot/webhook instructions said what to copy *out* of
  the panel (the token) but never what to paste *into* it.** Outgoing
  Webhooks → Create requires a Callback URL, and nothing — not the wizard,
  not the README, not `docs/` — said what it was; the only way to find out
  was reading `fastapi/main.py` for the route it defines. Step 9 now prints
  `http://fastapi:8000/webhook/mattermost` alongside the existing
  instructions, and `set-webhook-token`'s empty-token error carries the same
  hint for whoever skipped it during install. The URL lives once in
  `domain/stack.py` (`WEBHOOK_CALLBACK_URL`), next to `FASTAPI_SERVICE`, so
  the two call sites can't drift from the actual route.

## [0.3.2] — 2026-08-12

### Fixed

- **Every fresh install broke at step 6: Mattermost never left `unhealthy`,
  and the deployment error printed 30 lines of perfectly normal startup log
  with no indication of the real cause.** 0.3.1's move to Mattermost 11.7.8
  landed on a far more minimal image than 10.x's Ubuntu-based one: no shell,
  no coreutils, not even `curl` or `wget` — so the healthcheck's `curl -fsS
  http://localhost:8065/api/v4/system/ping` failed every single time with
  "executable file not found in $PATH", indistinguishable from the server
  itself never coming up. It had, in every case tested. The healthcheck now
  runs `mmctl system status --local` — the one binary the image still ships
  besides the server, and exactly what the image's own baked-in `HEALTHCHECK`
  already uses, talking over a local Unix socket rather than HTTP.
- `doctor`'s "Webhook reachable" check had the identical bug one layer up: it
  ran `curl` inside the mattermost container to prove `fastapi:8000` was
  reachable, which also always failed on 11.x. It now runs from `nginx`
  instead — same `edge_net`, same reachability fact, and nginx's image still
  carries curl. (Reachability between two containers on the same bridge
  network does not depend on which container asks, so this checks exactly
  the same thing it always did.)

### Verification

916 tests passing, `ruff` and `mypy` clean. Reproduced live: a fresh install
against 0.3.1's binary hung at step 6 exactly as described above; the same
install against this fix reached `healthy` on Mattermost's first healthcheck
after `docker compose up`, confirmed both with a minimal `postgres` +
`mattermost` compose stack and by exec-ing `mmctl system status --local`
directly against a running 11.7.8 container.

## [0.3.1] — 2026-08-12

### Breaking

- **Mattermost upgraded from 10.5.14 to 11.7.8.** The 10 line is a dead end:
  both of its ESRs are now out of support — 10.5 since 2025-11-15, 10.11 since
  2026-08-15 — so nothing found after those dates was ever backported to the
  pin. 11.7 is the ESR Mattermost's own docs name as the replacement, with
  support to 2027-05-15. This is the deliberate migration `domain/images.py`
  had been deferring, not a patch bump: it carries a database schema migration
  that does not come back. **Back up `postgres_data` and the `mattermost_*`
  volumes before running `install`/`doctor --fix` against an existing
  deployment.**

### Fixed

- **WireGuard could never come up on a kernel with no IPv6 netfilter, and the
  install could never get past step 6 because of it.** Docker Desktop's WSL2
  kernel commonly carries none at all, so the `ip6tables` commands wg-easy puts
  in its `PostUp` fail with "Table does not exist" — and `wg-quick` runs that
  hook as a single chain, so the failure aborted the whole thing and rolled the
  interface back (`ip link delete dev wg0`). Nothing looked broken from the
  outside: the container stayed up and the panel answered. But `wg show` was
  empty, so the image's own healthcheck failed forever, step 6 waits on all
  eight services being healthy, and the install stalled there with the real
  cause buried in the wireguard log. The VPN was genuinely dead, too — no
  interface, no tunnel.

  Step 1 now actually tests this (`detect.docker.docker_ipv6_netfilter_works`,
  in the same spirit as the existing GPU passthrough probes) rather than
  assuming it from the platform: assuming it only on Windows would have left
  Docker Desktop for macOS and Linux — which share the same affected engine —
  silently broken by it too, while a native Linux Engine, which almost always
  ships `ip6_tables` compiled in, would have paid for disabling IPv6 it never
  needed. When the probe finds it broken, `wireguard`'s compose section gets
  `DISABLE_IPV6=true`, which applies to an already-initialised volume as well,
  so enrolled clients survive the fix.

- **Step 6 reported nothing at all while it was building.** Buildkit's output
  names no service — 30 of the 48 lines a minimal build emits look like
  `#6 [2/2] RUN …` — and none of it was recognised, so every bar sat at
  "waiting…" for the whole build while Docker Desktop still showed no
  containers. On a first install that is many minutes indistinguishable from a
  hang. The two events that frame the build (` Image … Building `/` Built `)
  were being dropped too, because the parser read `Image` as the container's
  name. Both are now recognised, and buildkit's step is shown on the bar of
  whichever service is building.

- **A `docker compose up` that ran out of time could hang forever, and when it
  did not, it reported nothing useful.** Two separate faults met: the timeout
  raised `CommandTimeoutError`, a plain `RuntimeError`, and the step runner
  records only `AxionError` — so the failure never reached
  `.axion-wizard-state.json` and came out through the last-resort handler as
  `Unexpected error: 900.0s timeout exceeded running: docker compose …`. Worse,
  the abort path closed the pipe on the assumption that killing the child
  guarantees EOF, which only holds while the child owns the pipe's write end
  alone: a grandchild that inherited the handle keeps it open, the pending read
  never returns, and `close()` waits for exactly that read. Reproduced, and it
  blocks indefinitely — a timeout turning into a hang in the one code path whose
  job is to give up. The close is now bounded, and the timeout becomes a proper
  error panel that names which containers did come up.

### Changed

- **The limit for `docker compose up` goes from 15 minutes to an hour.** Not a
  cosmetic margin: without a TTY, Docker emits no byte-level progress — only
  `Pulling fs layer` → `Download complete` → `Pull complete` — so one large
  layer is a *single* silent stretch as long as the download itself. A first
  install pulls around eleven gigabytes (ollama alone is over six), and on a
  domestic line the wizard was killing its own deployment halfway through.
- **Container images.** ollama 0.32.6 → 0.32.9 (and `-rocm`), n8n 2.34.4 →
  2.35.1 — patch/minor bumps, no state to migrate. nginx's pin becomes the
  exact `1.31.3-alpine` instead of the floating `1.31-alpine` it resolved to
  anyway (same image digest) — consistent with every other pin here naming an
  exact patch. postgres, wg-easy and docker-volume-backup were already at the
  newest version within their pinned line.
- The fastapi bridge's own `Dockerfile` moves off the floating `python:3.12-
  slim` to the exact `python:3.12.13-slim` it already resolved to, same
  reasoning as the nginx pin above.
- `uv.lock` refreshed to the newest versions satisfying `pyproject.toml`'s
  bounds (platformdirs, pydantic-settings, ruff, mypy tooling, pyinstaller and
  their transitive dependencies). No dependency's lower bound changed; test
  suite, ruff and mypy all pass unchanged against the new resolution.

## [0.3.0] — 2026-08-11

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
  1.31-alpine, ollama 0.6.5 → 0.32.6 (and `0.32.6-rocm`), mattermost 10.5.1 →
  10.5.14. Mattermost stays on the 10 line and PostgreSQL on 15 deliberately: a
  major bump on either triggers migrations that do not come back, which is not
  something an installer should apply on its own.

  **Mattermost 10 is nonetheless a dead end**, and the patch bump only papers
  over it: 10.5 is an ESR whose support ended on 2025-11-15, so nothing found
  since has been backported. 10.11 ESR ends 2026-08-15. The supported
  destination is 11.7 ESR (to 2027-05-15) — a deliberate migration, not a pin
  change.
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
- `axion.toml` saved with a byte-order mark — what Notepad's "UTF-8" and
  PowerShell 5.1's `Out-File -Encoding utf8` both produce, so the obvious way
  to create it on Windows — was rejected with `Invalid statement (at line 1,
  column 1)`, pointing at a line that is perfectly correct. In step 8b the same
  cause was worse: the file was read inside a `try` returning `None`, so the
  bot and webhook tokens simply went missing with no error at all. Both now
  read with `utf-8-sig`.
- The build scripts' checksum dedupe never worked against `sha256sum`'s binary
  mode. Both filtered on a space before the filename, but binary mode — the
  default under Git Bash on Windows — writes `<hash> *name`, so each script
  walked past its own earlier line and left a stale entry for the same binary.
  `sha256sum -c dist/checksums.txt` then failed on a file that was perfectly
  fine. This is precisely the duplicate the filtering was added to prevent.
- `uv.lock` still declared `bcrypt` as a dependency after it was dropped from
  `pyproject.toml`, so `uv sync` — the reproducible path CI and the build
  scripts prefer — kept installing it. Regenerated; it also picked up
  `textual`, `fastapi` and `python-multipart`, which had never made it into the
  lock.

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
