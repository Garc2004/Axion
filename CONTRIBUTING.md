# Contributing

## Setting up

Everything goes through the bootstrap script, which knows how to build the
environment with `uv` if it is present and with `venv` + `pip` if not:

```bash
./scripts/bootstrap.sh --no-run        # Linux/macOS/WSL
```

```powershell
.\scripts\bootstrap.ps1 -NoRun         # Windows
```

With `make` available, `make setup` does the same thing.

## Before opening a pull request

Three gates, all of which must pass:

```bash
.venv/bin/python -m ruff check .       # lint
.venv/bin/python -m mypy src           # types
.venv/bin/python -m pytest -q          # tests
```

`make check` runs all three. On Windows the interpreter is
`.venv\Scripts\python.exe`.

There is no coverage threshold, but a change that alters behaviour is expected
to come with a test that fails without it.

## Conventions this codebase follows

### Errors carry what, why and steps

Every failure the user can reach is an `AxionError` subclass with three
mandatory fields:

```python
raise ConfigError(
    what="Configuration file axion.toml was not found",
    why="`--unattended` needs it in order to know what to deploy.",
    steps=["Check the path given to --config: /srv/axion/axion.toml"],
)
```

A raw traceback only ever reaches the user under `--verbose`. If you add a
failure path, it needs all three fields — "what" alone is not enough, and
neither is a message that says what happened without saying what to do.

### Comments explain why, not what

Most of the long comments here document a real incident and the reason a
seemingly odd choice is the correct one: why `curl` and not `wget`, why the
GPU is probed rather than trusted, why the WebSocket handshake is done with a
raw socket, why `os.walk` rather than `Path.glob`. They are load-bearing —
several of them exist because the alternative fails silently, which means the
next person to touch that code has no way to rediscover the reason.

If you remove one, remove the reason with it. If you change the behaviour it
describes, update the comment in the same commit.

### Nothing is written before step 3's confirmation

The install flow does not touch the disk until the user has seen a summary and
confirmed it. Keep it that way: a step that writes earlier makes cancelling
leave a half-built deployment behind.

### Secrets never reach the console or the logs

`utils.secrets.register_secret` marks a value for redaction, and
`utils.secrets.redact` is applied at the boundary — in `services.compose.logs`,
not at each print site — because PostgreSQL and Mattermost log their full DSN
when they fail to connect. The persisted state file records only *which* steps
finished, never their values.

### Steps are idempotent, verifiable and restorable

A `Step` implements `run()`, `verify()` and optionally `restore()`:

- `run()` applies it, and must be safe to run twice.
- `verify()` checks it is still applied, without modifying anything. It is
  what `doctor` reuses, and what catches a step whose result no longer exists.
- `restore()` rebuilds what the step contributed to the context by reading
  `.env`/`wg.env`/`docker-compose.yml`. It is what makes resuming possible
  without persisting any secret.

### Images are pinned

Never `latest`. `assert_no_unpinned_images` fails the render if one slips in.
For wg-easy the pin is stricter still: `assert_wg_easy_tag_is_safe` rejects any
major other than the one the wizard knows how to configure, because each major
configures itself incompatibly and getting it wrong produces no error — the
panel starts and simply no credential works.

### Dependencies are argued for, not assumed

The wizard ships as a PyInstaller binary, so every runtime dependency has a
bundle cost, and several modules that *look* like hand-rolled clients are
deliberately so — their contracts (not raising on a timeout, redacting at the
boundary, streaming lines to a progress bar) are what a general-purpose library
does not give.

Before adding one, check whether it has already been evaluated and rejected in
[docs/dependency-evaluation.md](docs/dependency-evaluation.md), which records
what each candidate would actually replace, measured against this repository.

## Layout

See the README's "Architecture" section for the full tree. The short version:

```
cli.py  →  commands/  →  steps/orchestrator  →  steps/sNN_*
                     ↘   services/  ↘  detect/  ↘  domain/  ↘  utils/
```

- `domain/` is what the stack *is*: config, service names, image tags, and
  reading an existing deployment back. It depends on almost nothing, because
  everything depends on it.
- `render/` is how output looks, shared by the Rich CLI and the Textual TUI.
- `detect/` only reads. `services/` does I/O against external systems.
- `commands/` is split by what the user is trying to do, not by which service
  is involved.

Put a new subcommand's implementation in `commands/`, and re-export its
`run_*` from `commands/__init__.py` so `cli.py` keeps importing from one place.
Keep the heavy imports inside the function body — `--version` should not pay
for loading httpx, cryptography and questionary.

## Language

The project is in English: code, comments, docstrings, CLI output and
documentation. References to the original specification (`§4.3`, `§6.4`…) are
kept verbatim — they point at a document that is not translated.

## Commits

One logical change per commit, with a message that says what changed and
**why**. The body is where the reasoning goes; several commits in this
project's history are the only record of an incident that motivated a fix.

Behaviour-breaking changes say so explicitly in the body and get an entry in
`CHANGELOG.md` under **Breaking**.
