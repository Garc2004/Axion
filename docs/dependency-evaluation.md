# Third-party libraries: what was evaluated, and why it was not adopted

A record of an evaluation carried out on 2026-08-13, so the next person to ask
"should we not just use a library for this?" does not have to redo the
measurements. Every figure below was counted against this repository at that
date, not estimated.

The short version: **four libraries were evaluated and one was recommended.**
The three rejected were rejected for the same underlying reason — the code they
would replace is small, and the behaviour around it is deliberately
non-standard in ways each library does not offer.

| Library | Verdict | Would replace | Cost |
|---|---|---|---|
| [pytest-textual-snapshot](https://github.com/Textualize/pytest-textual-snapshot) | **Recommended** | Nothing (dev-only) | None at runtime |
| [ollama-python](https://github.com/ollama/ollama-python) | Rejected | 61 of 428 lines (14%) | A second HTTP stack |
| [python-on-whales](https://github.com/gabrieldemarmiesse/python-on-whales) | Rejected | ~40–60 lines net | Rewrites 4 documented contracts |
| [textual-autocomplete](https://github.com/darrenburns/textual-autocomplete) | Rejected | ~40 lines | Loses the always-visible list |

---

## Recommended: pytest-textual-snapshot

A pytest plugin from Textualize. It renders the app to an SVG and compares it
against a committed snapshot, failing on any visual change.

**Why it fits here.** The TUI tests assert against Textual's widget internals,
and that has already cost work. `tui/app.py` carries a `StepLine.text` property
whose docstring says exactly why:

> *"It exists so the tests can check what the line shows without depending on
> how Textual stores a `Static`'s content, **which has changed between
> versions**."*

That is a workaround for a problem this plugin solves properly. The same
problem recurred during the 2026-08-13 session: a new test written against
`Static.renderable` failed with `AttributeError` on Textual 8.2.8 and had to be
rewritten against `.content`.

**What it does not do.** It does not reduce application code, and it does not
reduce test code — it *adds* SVG snapshots to the repository. Its value is
robustness, not brevity. Adopt it for that reason or not at all.

**Caveat before adopting.** Snapshots are sensitive to terminal size and to
anything non-deterministic on screen. The form already renders a `project_dir`
path that varies per machine (`tmp_path` under pytest), so snapshot tests would
need a fixed directory or that line masked.

---

## Rejected: ollama-python

The official Ollama client. The obvious candidate, since `services/ollama.py`
is 428 lines of what looks like a hand-rolled client.

**It is not.** The module has 18 top-level definitions (16 functions, 2
dataclasses). Only three make any HTTP call at all, and only these could be
handed to the SDK:

| Function | Lines | Note |
|---|---|---|
| `list_installed_models` | 14 | `GET /api/tags` on the local server |
| `pull_model` | 24 | `POST /api/pull`, streaming |
| `_handle_pull_line` | 23 | parses that stream |
| **Total** | **61 of 428 (14%)** | |

The other 367 lines are the three-tier catalogue, hardware-fit scoring,
suitability reasoning and remote-catalogue parsing. The SDK has none of that.

Two specific blockers, either of which is sufficient on its own:

1. **`fetch_remote_catalog` queries `https://ollama.com/api/tags`** — the public
   *website*, not a local Ollama server. It is the module's fourth HTTP caller
   and the SDK cannot address it at all, so `httpx` stays a dependency of this
   file regardless of what else changes.
2. **`list_installed_models` must not raise.** Its docstring: *"Does not raise
   if Ollama is not running or does not answer in time — it returns an empty
   list, which is valid input for the rest of the flow."* The official client
   raises. Every call site would need wrapping, which is the code the SDK was
   supposed to remove.

It would also add a **second HTTP stack**: `httpx` is already a dependency and
is used by `s09_verify` and `services/wireguard` as well as here.

---

## Rejected: python-on-whales

A typed wrapper over the Docker CLI. Considered against `services/compose.py`
(347 lines).

Roughly 134 of those lines are subprocess wrapping and 213 are domain logic
that stays regardless (`ContainerStatus` semantics, `describe_service_state`,
`build_up_timeout_error`, `build_deployment_failure_error`). But all four
wrappers behave deliberately unlike a general-purpose Docker library, and each
deviation is documented in the source from a real incident:

- **`ps` returns `[]` on timeout** instead of raising, because it is called
  from inside `wait_for_healthy`'s retry loop, where a slow daemon reply means
  "not ready yet". Letting the timeout escape made a slow daemon look like a
  wizard crash and left nothing in the state file.
- **`logs` redacts at the boundary**, because PostgreSQL and Mattermost log
  their full DSN — password included — when a connection fails, and that log
  reaches both the error panel and the log file.
- **`up` streams lines** to `on_line`, which is what drives the per-service Rich
  progress bars and the buildkit fallback in `s06_deploy`.
- **`config_validate` redacts stderr**, because Compose interpolates `.env`
  before validating and can quote a line with the password substituted in.

python-on-whales raises `DockerException` and returns its own object types, so
each of these would become adapter code. Realistic net saving: **40–60 lines**,
in exchange for a dependency that shells out to the same `docker` binary this
module already calls, and for rewriting four contracts that each exist because
something went wrong once.

`services/compose.py`'s own module docstring already records the related
decision against docker-py, and for a similar reason.

---

## Rejected: textual-autocomplete

Would replace the `ModelCombo` widget in `tui/app.py` (~40 substantive lines).

Rejected on **product** grounds rather than cost. It provides a dropdown that
appears *while you type*. `ModelCombo` shows an always-visible ranked list —
which is the entire point of the feature, because the user is not expected to
know what to type. Seeing `✓ llama3.1:8b — 4.7 GB — compatible` next to
`llama3.1:70b — 40.0 GB — needs a dedicated GPU` is what makes the choice
informed. A type-to-reveal dropdown gives that only to someone who already
knows a name to start typing.

Secondary: the wizard ships as a PyInstaller binary, so every runtime
dependency has a bundle cost. `ModelCombo` is built entirely from Textual
built-ins (`Input(suggester=SuggestFromList(...))` plus `OptionList`), both
available in the pinned Textual 8.2.8.

---

## Also surveyed, not applicable

- **[trogon](https://github.com/Textualize/trogon)** — generates a Textual
  *command builder* from a Typer app. Wrong shape: `install --tui` is a guided
  install wizard, not a form for assembling a command line.
- **[Coolify](https://github.com/coollabsio/coolify) (~57k ★),
  [Dokploy](https://github.com/Dokploy/dokploy) (~35k ★), CapRover (~15k ★)** —
  self-hosted PaaS platforms. Useful as prior art for features, but they *are*
  the deployment platform; this wizard installs one specific stack onto a
  machine the user already has. Not adoptable, worth reading for ideas.

---

## The better target: duplication already in this repository

The evaluation's most useful outcome was not a library. This triple appears
**six times** in `steps/s01_environment.py`:

```python
self.context.warn(message)
console.print()
console.print(f"[axion.warn]{message}[/]")
```

It has already caused a shipped bug. From the 0.3.4 entry in `CHANGELOG.md`:

> *"One of the four (client isolation on the router, when mirrored networking
> already looks correctly configured) was being recorded for the closing
> summary but never actually printed live — **the only one of six similar
> warnings in that file missing its `console.print`**."*

A warning was invisible to the user because one hand-written copy of a
three-line pattern was incomplete, while still being recorded for the closing
summary — so it looked correct in the code and in the final panel, and only
failed in the place it mattered.

A `warn_and_show(message)` helper on `Step` (or on `InstallContext`, which
already owns `warn`) makes that class of bug structurally impossible and
removes about twelve lines. There are 13 `context.warn` call sites across
`src/`, so the pattern is not confined to step 1.

**Implemented** as `Step.warn_and_show` in `steps/base.py`. It went on `Step`
rather than on `InstallContext` to keep the context a pure data carrier, and
because the two names now draw a line worth having: `context.warn` records,
`warn_and_show` records *and* shows.

Eight of the thirteen call sites were the full triple and were converted — the
six in `steps/s01_environment.py` plus one each in `s06_deploy.py` and
`s08_wireguard.py`, which printed the same line without the leading blank. The
other five never printed on purpose (step 2's is printed by its confirmation
prompt; steps 7 and 8b carry theirs out in a `StepResult`) and still call
`context.warn`.

One detail worth keeping: the six existing tests over these warnings all
asserted on `context.warnings`, never on what was printed — which is precisely
why the 0.3.4 bug reached a release. The list was correct. Tests over the
printed output were added with the helper, and were checked by reintroducing
the missing `console.print` and confirming they fail.

---

## How to redo these measurements

```bash
# Function sizes in a module
.venv/Scripts/python.exe -c "
import ast; from pathlib import Path
t = ast.parse(Path('src/axion_wizard/services/ollama.py').read_text(encoding='utf-8'))
for n in t.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        print(f'{n.end_lineno - n.lineno + 1:>5}  {n.name}')
"

# Where a duplicated pattern actually occurs
grep -rn "context\.warn(" --include=*.py src/
```
