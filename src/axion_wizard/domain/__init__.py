"""What the AXION stack *is*, independent of how it gets deployed.

The four modules here answer questions that have nothing to do with the
install flow, the terminal, or Docker being reachable:

- `config`     — the validated shape of an install (`AxionConfig`)
- `stack`      — which services the stack is made of
- `images`     — which container image tag each of them is pinned to
- `deployment` — how to read an existing deployment back off disk

They are imported by `steps/`, `commands/`, `services/` and `tui/` alike, so
they deliberately depend on almost nothing: `errors`, and each other.
"""
