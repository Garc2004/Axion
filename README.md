<p align="center">
  <img src="assets/logo.png" alt="axion-wizard" width="180">
</p>

# axion-wizard

Installer and orchestrator for the AXION stack: Mattermost + WireGuard +
Ollama + a FastAPI bridge, on Docker Compose. It ships as a self-contained
binary, runs ten steps with persisted progress, and every error it can produce
tells you what happened, why it matters, and what to do about it.

## Getting started

One command sets up the environment, installs the dependencies and starts the
wizard. It is idempotent: running it again breaks nothing.

**Windows (PowerShell)**

```powershell
.\scripts\bootstrap.ps1
```

**Linux / macOS / WSL**

```bash
./scripts/bootstrap.sh
```

The script looks for a Python >= 3.11, creates `.venv` and installs
everything. It uses [uv](https://docs.astral.sh/uv/) if it finds one
(respecting `uv.lock`, so the environment is reproducible) and falls back to
`venv` + `pip` if not. It does not install uv on your behalf.

If something fails, the error says what happened and what to do — there is no
stack trace to interpret.

### Passing arguments to the wizard

Whatever comes after reaches the wizard verbatim:

```powershell
.\scripts\bootstrap.ps1 doctor            # Windows
```

```bash
./scripts/bootstrap.sh -- doctor          # Linux/macOS
```

### Other variants

| Goal | Windows | Linux/macOS |
|---|---|---|
| Prepare the environment only | `.\scripts\bootstrap.ps1 -NoRun` | `./scripts/bootstrap.sh --no-run` |
| Prepare + lint/types/tests | `.\scripts\bootstrap.ps1 -Check -NoRun` | `./scripts/bootstrap.sh --check --no-run` |

With `make` available: `make setup`, `make check`, `make test`, `make lint`,
`make run ARGS="doctor"`, `make build`, `make clean`. `make help` lists them.

## The install flow

`axion-wizard install` runs ten steps in order and **persists progress after
each one**: if it is interrupted, the next run resumes from the last completed
step rather than starting over.

| # | Step | What it does |
|---|---|---|
| 1 | Environment | OS, WSL, Docker, hardware → decides the WireGuard variant (`host` or `ports`) |
| 2 | Network | LAN IP, CGNAT, free ports, outbound connectivity |
| 3 | Configuration | Prompts with live validation + a summary and **one single** confirmation |
| 4 | Certificate | TLS with `subjectAltName`, verified by reading the file back |
| 5 | Compose | `docker-compose.yml`, `.env`, `wg.env`, nginx and the FastAPI bridge |
| 6 | Deployment | `up -d --build` and healthcheck waiting with backoff |
| 7 | Model | Model download with a real progress bar |
| 8 | WireGuard | First client and its QR drawn in the terminal |
| 9 | Bot and webhook | Asks for the Mattermost tokens (optional, see below) |
| 10 | Verification | The same checks `doctor` runs |

**Nothing** is written to disk until step 3's confirmation: cancelling before
that leaves nothing half-done.

State lives in `.axion-wizard-state.json` and records only *which* steps
finished, never their values — no secret is persisted there. On resume, each
step rebuilds its own part by reading `.env` and `wg.env`.

### Resuming does not mean trusting the file

That file says what happened **last time**, not what is true now: between two
runs, Docker may have been uninstalled, the containers deleted, or the project
moved. So before taking a step as done, the wizard **checks** it; if it no
longer holds, it redoes it — and every step after it, because they were built
on top.

Without that, this happened, and it is a real case: the state said
`deploy: 6 services operational` after Docker was uninstalled, `install`
skipped the deployment — "it was already done" — and landed on step 9 to fail
all seven checks, with no hint that the problem was seven steps earlier.

On startup, if there is saved progress, the whole map is shown: what is done,
what failed, and where it will pick up.

**What that check does not catch.** It verifies that last time's result still
stands, not that it matches what this wizard would generate *today*: step 4
accepts any certificate that has some SAN, and step 5 only checks the files
exist, not their contents. So after upgrading the wizard, a bare `install` may
apply nothing and finish green. To get template changes into the deployment
you need `axion-wizard install --restart`, which redoes everything from step 1
— and regenerates the certificate, so the browser will warn once more.

### Starting from scratch

```bash
axion-wizard reset               # forget the progress; the next install goes to step 1
axion-wizard install --restart   # both at once
```

`reset` deletes **only** the step record: no containers, no volumes, no
`.env`, no certificate. Redoing the install over an existing deployment is
safe because step 3 reuses the PostgreSQL password already in `.env`. To
really delete the data, use `axion-wizard uninstall --purge`.

Re-running `install` on an already-deployed project is safe: the PostgreSQL
password, the webhook token and the AI's instructions are carried over from
the previous `.env` (Postgres only applies its password when initialising the
volume; regenerating it would leave Mattermost unable to log in).
`docker-compose.yml`, `.env` and `wg.env` are backed up with a `.bak` suffix
before being rewritten.

### Unattended mode (CI)

```bash
axion-wizard install --unattended --config axion.toml
```

```toml
access_mode = "lan"                          # "lan" | "domain"
host = "192.168.1.50"
ollama_model = "qwen2.5:1.5b"
wireguard_admin_password = "a-long-panel-password"   # min. 12 characters
# wireguard_admin_username = "admin"         # optional; defaults to "admin"
# postgres_password = "..."                  # optional; generated (hex) if absent
# mm_bot_token = "..."                       # optional; if known in advance
# mm_webhook_token = "..."                   # (e.g. reinstalling onto the same Mattermost)
# ai_reply_in_thread = false                 # optional; only with mm_bot_token set
```

Without those, step 9 (bot and webhook) is simply skipped — there is no prompt
to make without a terminal, and they are applied afterwards with
`set-bot-token`/`set-webhook-token` exactly as on the interactive path.

### Full-screen interface

```bash
axion-wizard install --tui
```

A form for the configuration and a screen with the ten steps and their log. It
is **an alternative, not the default path**: §1.3 of the spec rules Textual
out for the linear flow and that decision stands. It cannot be combined with
`--unattended` or with redirected input.

## Wizard commands

```
axion-wizard                      Full install flow
axion-wizard reset                Forget progress: the next install goes to step 1
axion-wizard doctor               Re-validate a deployed stack, without touching it
axion-wizard network-check        Only the network checks (§4.2)
axion-wizard gen-cert <host>      Generate the TLS certificate
axion-wizard model                Which model and instructions the AI uses now
axion-wizard model choose         Pick the model from a list and apply it
axion-wizard model set <n>        Change the model: pull + .env + recreate
axion-wizard model prompt "<t>"   Edit the AI's standing instructions
axion-wizard set-bot-token <t>    Remove the 30s limit: answers when it finishes
axion-wizard models               Ollama models compatible with this hardware
axion-wizard models pull <n>      Download a model (without activating it)
axion-wizard wireguard add-client <n>   Create a client and show its QR
axion-wizard up [service]         docker compose up -d (restarts nginx if needed)
axion-wizard down                 docker compose down
axion-wizard logs [service]       Last lines of each service's log
axion-wizard uninstall [--purge]  Bring the stack down (--purge deletes the volumes)
```

Options for `install`: `--unattended`, `--config <axion.toml>`, `--tui`,
`--restart`.

Global options (they go **before** the subcommand): `--verbose`, `--quiet`,
`--no-color`, `--dry-run`, `--yes`, `--no-elevate`, `--project-dir <path>`.

```bash
axion-wizard --project-dir /srv/axion doctor    # correct
axion-wizard doctor --project-dir /srv/axion    # error: No such option
```

## Architecture

```
src/axion_wizard/
  cli.py            Typer app: options, subcommands, the error panel
  errors.py         AxionError(what/why/steps) — the actionable-error contract
  privileges.py     UAC/sudo elevation and the relaunch

  commands/         What each subcommand does
    install.py        install, reset
    diagnose.py       doctor, network-check, gen-cert
    ai.py             models, model, the two Mattermost tokens
    vpn.py            wireguard add-client
    lifecycle.py      up, down, logs, uninstall

  domain/           What the stack *is*, with no I/O beyond its own artifacts
    config.py         AxionConfig — the validated shape of an install
    stack.py          Which services the stack is made of
    images.py         Which image tag each is pinned to
    deployment.py     Reading an existing deployment back off disk

  render/           How it looks on a terminal (shared by the CLI and the TUI)
    console.py        The Rich Console and the axion.* theme
    ui.py             Status glyphs and the report-table factory

  detect/           Read-only probes: platform, docker, hardware, network
  services/         I/O adapters: certs, compose, hostnet, ollama, wireguard
  steps/            The install flow: base, context, orchestrator, s01…s09
  templates/        Jinja2 templates + the FastAPI bridge's sources
  tui/              The Textual alternative to the questionary flow
  utils/            fsperms, jsonio, resources, secrets, shell, state, winconsole
```

The flow of control is one direction only:

```
cli.py  →  commands/  →  steps/orchestrator  →  steps/sNN_*
                     ↘   services/  ↘  detect/  ↘  domain/  ↘  utils/
```

Three conventions worth knowing before changing anything:

- **`Step` has `run()`, `verify()` and `restore()`.** `verify()` is what lets
  `doctor` reuse the install's checks, and what catches a step whose result no
  longer exists. `restore()` rebuilds a completed step's contribution to the
  context by reading `.env`/`wg.env` — that is what makes resuming possible
  without persisting any secret.
- **Errors carry `what` / `why` / `steps`.** A raw traceback only ever reaches
  the user under `--verbose`. If you add a failure path, it needs all three.
- **Comments explain *why*, not *what*.** Most of the long comments in this
  codebase document a real incident and the reason a seemingly odd choice is
  the right one. They are load-bearing.

## Editing the AI

Changing the model is three things, not one: pulling it, pointing
`OLLAMA_MODEL` at it, and **recreating** the FastAPI container (a `restart`
will not do — environment variables are fixed when the container is created,
so the old value survives a restart). Forgetting the third leaves an AI still
answering with the previous model, with no error at all.

`axion-wizard model` does all three:

```bash
axion-wizard model                       # what it uses right now
axion-wizard model choose                # pick from a list matched to your hardware
axion-wizard model set llama3.2:3b       # or straight by name
```

The standing instructions — tone, language, what it is and what it must not do
— are edited the same way, and apply to every conversation without repeating
them:

```bash
axion-wizard model prompt "You are AXION's internal assistant. Be brief."
axion-wizard model prompt ""             # clear them
```

### Letting the AI take as long as it needs

Mattermost waits for the outgoing webhook's HTTP response and **abandons it
after ~30 seconds**. A 7B model on CPU passes that without effort, so the
answer is lost whole: from the outside it looks as though the AI does not
reply, and there is nothing in the logs to explain it — internally the model
answered fine.

The fix is for the bridge to answer the webhook immediately and post the reply
to the channel once the model finishes. That needs a bot:

1. Mattermost → **Integrations → Bot Accounts → Create** (if the option is
   missing: System Console → Integrations → enable bot accounts).
2. Copy its token. A newly created bot **belongs to no team** — adding it
   straight to a channel fails with *"1 user was not selected because they are
   not a part of this team"*. First: System Console → User Management → Teams →
   the channel's team → Add People → search for the bot's username (`@axion`,
   not its display name). Only then can it be added to the channel.
3. Paste it into step 9 of `install` itself (it asks for exactly this), or
   afterwards with `axion-wizard set-bot-token <token>`.

`install` cannot create the bot for you — Mattermost exposes no API without a
session already opened by a human admin, and that account is created in the
web interface — but it does stop mid-install to ask for the token as soon as
you have it, rather than leaving it for later. Leaving it blank there breaks
nothing: it is applied later in exactly the same way.

From then on there is no time ceiling and you can use whatever model the
hardware carries. Without a token, the bridge keeps working in synchronous
mode exactly as before.

With the bot set, step 9 also asks whether the reply should hang off the
message that triggered it — in a thread, collapsed until clicked — or be
posted as a normal channel message (`AI_REPLY_IN_THREAD` in `.env`, threaded
by default). It has no effect in synchronous mode: there the decision belongs
to Mattermost's own outgoing-webhook mechanism, not to this code. Changing it
after installing means editing that line in `.env` and running
`axion-wizard up fastapi`.

While that token is not set, the wizard raises the deadline Mattermost grants
the webhook from its default 30 seconds to **180**
(`MM_SERVICESETTINGS_OUTGOINGINTEGRATIONREQUESTSTIMEOUT`). It does not replace
asynchronous mode — the request still waits — but it is the difference between
a mid-sized model on CPU working and losing every answer. As a reference
measured on an i3-10100F with no GPU: `qwen2.5:0.5b` generates 19 tokens/s and
`qwen2.5:3b` 4.6, i.e. ~43 seconds for a 200-token answer.

## Upgrading to wg-easy v15

**This is a breaking change for existing deployments.** From version 0.3.0 the
wizard installs wg-easy **v15**, which is a ground-up rewrite of the project:

| | v14 | v15 |
|---|---|---|
| Configuration | `WG_HOST`, `PASSWORD_HASH` (bcrypt) | `INIT_*` variables, **first boot only** |
| Panel password | a bcrypt hash | **plaintext**, minimum **12 characters** |
| Username | none | **required** (defaults to `admin`) |
| Plain HTTP | default | requires `INSECURE=true` |

**v15 cannot read a v14 data volume.** If it is allowed to start on one it
does not fail: it finds a store it does not recognise, launches its setup
wizard, and leaves an empty panel — every already-enrolled client stops
connecting at once, with nothing in the logs to say there was data to migrate.

`axion-wizard install` detects this and refuses to proceed. To migrate:

1. Open the **v14** panel and use its backup button to download `wg0.json`.
2. Run `axion-wizard install` again. The v15 panel comes up in its setup
   wizard.
3. Choose "I already have a configuration file" and upload `wg0.json`.

If there are no clients worth keeping, `axion-wizard uninstall --purge`
removes the volume and the next install starts clean.

The panel password now has a **12-character minimum**. It is not our policy:
it is what wg-easy's own login validation enforces. A shorter one creates the
account anyway — `INIT_PASSWORD` does not validate length — and then no login
ever passes, with the panel returning a 400 that looks like "wrong password".
The wizard checks it at the prompt, where it can still be corrected.

## Backups

The `backup` service archives the volumes into `backups/`, inside the
project's own directory, with nothing to configure.

```
BACKUP_CRON_EXPRESSION=0 3 * * *   # in .env; `install` keeps whatever you set
BACKUP_RETENTION_DAYS=7
```

Apply changes with `axion-wizard up backup`. To take a backup right now:

```bash
docker exec axion-backup-1 backup
```

Two things worth knowing before they happen:

- **PostgreSQL and Mattermost are stopped during the backup**, for a few
  seconds, and started again on their own. Copying a running database's data
  directory produces an archive that may not restore; hence the small-hours
  default.
- **`ollama_data` is not backed up** — gigabytes of model recoverable with
  `axion-wizard model set` — nor are Mattermost's logs. The database, uploaded
  files, configuration, plugins and WireGuard keys all are.

Age-based pruning only reaches files beginning with `axion-`, so anything else
can safely be left in that folder.

## n8n

Included natively, with no flag: `install` deploys it alongside the rest. It
lands on `http://<host>:5678`, on its own port and **not behind nginx**, just
like the WireGuard panel: nobody terminates TLS for it, so it announces itself
as `http` on purpose — saying `https` would make it generate webhook URLs that
do not answer.

Three things the wizard handles for you that cost dearly by hand:

- **`n8n:5678` is in Mattermost's allowed-destinations list.** Without it, the
  SSRF protection drops the outgoing webhook **silently**: it does not fire and
  no error appears in any log. Since that variable lives in a managed service,
  setting it by hand would be overwritten by the next `install`.
- **`N8N_ENCRYPTION_KEY` is generated once and preserved.** If it changes,
  every credential stored in n8n becomes unreadable forever; n8n starts anyway
  and the workflows fail to authenticate without saying why.
- **Its volume is in the backups.** Otherwise a restore would bring back the
  entire chat and leave n8n empty.

Set `N8N_TIMEZONE` in `.env` to an IANA name
(`America/Argentina/Buenos_Aires`, `Europe/Madrid`): with a value it does not
recognise, n8n stays on UTC and scheduled workflows fire at a different hour
without warning.

Inside the stack's network, n8n sees Ollama at `http://ollama:11434` and
Mattermost at `http://mattermost:8065`.

## GPU

The wizard does not trust that a GPU exists: it **tests** that Docker can hand
one to a container before reserving it, because reserving it without checking
leaves `ollama` stuck in `created` forever and drags `fastapi` down with it.

| GPU | What happens |
|---|---|
| NVIDIA | Tests `--gpus all`. Needs `nvidia-container-toolkit`. |
| AMD | Tests `/dev/kfd` and `/dev/dri`, and uses the Ollama image built against ROCm. Needs the `amdgpu` module and membership of the `video` and `render` groups. |
| Intel | Detected and warned about: Ollama publishes no image for its GPUs, so it runs on CPU. |

If the test fails, nothing breaks: the model runs on CPU and the warning says
what to check in each case.

## Where it writes its files

Without `--project-dir`, the wizard never writes into the directory the bare
binary is run from: if that directory already holds a deployment
(`docker-compose.yml` present) it is used as-is, and if not, an `axion/`
subdirectory is created there and used. Running the freshly downloaded `.exe`
straight from `~/Downloads`, for example, creates `~/Downloads/axion/` rather
than scattering `docker-compose.yml`, `.env`, `nginx/`… loose in Downloads.

To choose the folder by hand: `axion-wizard --project-dir <path> install`.

## Every deployment has its own project name

`.env` carries `COMPOSE_PROJECT_NAME`, generated once and preserved by every
later `install`. It is what stops **two separate installs on the same Docker
host** from ending up sharing containers and volumes: Compose identifies a
project by its name, not by the directory it was invoked from, so without a
unique name per deployment, installing into a second folder would reuse the
first one's containers and volumes — same Postgres, same Mattermost, same
database — and each `install` would silently overwrite the other's
configuration.

This is not hypothetical: it happened while developing this project. A fresh
install generated a different PostgreSQL password from the one the volume was
already initialised with, and Mattermost ended up in a restart loop
authenticating with the old password against a `.env` holding the new one,
with no log pointing out that the real problem was a collision between two
installs. Data is not lost in this scenario — Postgres ignores a new password
when the volume was already initialised — but the stack will not come up until
you settle which `.env` wins.

**Never copy `COMPOSE_PROJECT_NAME` from one deployment to another.**

### Moving the deployment to another folder

Copying the files and bringing it back up is enough: the project name travels
in `.env` and does not depend on the path. If you are coming from an install
with the old `docker-compose.yml` (versions before this change pinned
`name: axion` there, the same for everybody), the wizard migrates that value
into `.env` automatically on the first `install` after upgrading — no flags, no
manual steps.

## If the AI only answers on reload (F5)

That symptom is not the AI failing to answer: it answers, and the message
never reaches the browser. Mattermost pushes new messages over a **WebSocket**,
and reloading the page re-fetches them over ordinary HTTP — which is why they
all appear at once. In other words: HTTP healthy, WebSocket broken.

```bash
axion-wizard doctor    # look at the `Mattermost WebSocket` row
```

That check performs the handshake for real and separates the two causes, which
call for opposite fixes:

| Result | Cause | What to do |
|---|---|---|
| Rejected with HTTP 4xx | `MM_SITEURL` does not match the host the browser uses, or nginx is missing the `Upgrade`/`Connection` headers | Fix `MM_SITEURL` in `.env` and run `axion-wizard up` |
| No answer / cut off | Open WSL2 bug with `networkingMode=mirrored`: long TCP connections stall ([moby/moby#48201](https://github.com/moby/moby/issues/48201)) | Go back to NAT + `netsh portproxy`, or live with the F5 |

Mirrored is what gives access from a phone and other machines on the LAN, so
going back to NAT has its own cost: it is worth confirming which of the two
causes it is before changing anything.

More failure modes, and what they look like from the outside, are in
[docs/troubleshooting.md](docs/troubleshooting.md).

## Privileges

`install`, `up`, `down` and `uninstall` need administrator rights (firewall,
`sysctl`, `netsh portproxy`). The wizard explains why before asking, and
relaunches the process elevated:

- **Windows**: opens a new process through UAC — Windows does not allow
  elevating one already running — and the original process **waits for it to
  finish** in order to propagate its exit code. The elevated window asks for
  Enter before closing, so its output can be read.
- **Linux/macOS**: `sudo -E` in the same terminal.

`--no-elevate` carries on without privileges (some steps will fail) and
`--dry-run` never elevates, because it touches nothing.

On native Linux (the `host` variant) the privileges are used for one concrete
thing: writing `/etc/sysctl.d/99-wireguard.conf` and enabling IP forwarding.
Without it, the WireGuard tunnel establishes, the handshake works and the panel
shows the client connected — but not one packet gets through, and no error
appears in any log. `axion-wizard doctor` checks it in the
`IP forwarding (WireGuard)` row.

Environment variable `AXION_NO_PAUSE=1` disables the "Press Enter to close"
pause. Useful in CI or in wrappers. The pause already disables itself when the
output is not an interactive terminal.

## Packaging

Produces a self-contained binary, with no Python on the target machine:

```powershell
.\build\build.ps1        # -> dist\axion-wizard.exe
```

```bash
./build/build.sh         # -> dist/axion-wizard-linux-x86_64
```

There is no cross-compilation: each platform builds its own. The script leaves
the SHA-256 in `dist/checksums.txt`, verifiable with `sha256sum -c`.

## Development

```bash
.venv/bin/python -m pytest -q          # tests
.venv/bin/python -m ruff check .       # lint
.venv/bin/python -m mypy src           # types
```

On Windows, `.venv\Scripts\python.exe`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the conventions this codebase
follows and what a change is expected to come with.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

The `.exe`/binary produced by PyInstaller bundles third-party dependencies
under the MIT, BSD-3-Clause and Apache-2.0 licences; their copyright notices
are in [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt).
