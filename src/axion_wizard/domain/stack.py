"""Service names of the AXION stack.

These names are stack vocabulary, not a property of whichever install step
happens to touch a service first. Before this module they lived scattered
across the step modules, and three of them were defined *twice* —
`fastapi` in both `s07_model` and `s09_verify`, `wireguard` and `nginx` in
both `s06_deploy` and `s09_verify`. Two definitions of the same string is
one rename away from a bug that no type checker can catch: the deploy path
would wait on `wireguard` while the verify path checked something else.

`MANAGED_SERVICES` lived in `s05_compose` for the same accidental reason —
it is the module that renders them — and `s06_deploy` and `steps/runner`
both had to reach into a step module to ask "what is this stack made of?".

Keep this module dependency-free. It is imported from the compose renderer,
the deployer and the verifier, and anything it imports would be pulled into
all three.
"""

from __future__ import annotations

POSTGRES_SERVICE = "postgres"
MATTERMOST_SERVICE = "mattermost"
OLLAMA_SERVICE = "ollama"
FASTAPI_SERVICE = "fastapi"
NGINX_SERVICE = "nginx"
WIREGUARD_SERVICE = "wireguard"
BACKUP_SERVICE = "backup"
N8N_SERVICE = "n8n"

#: Services the wizard regenerates on every render. Any other service the
#: user added by hand to an existing compose file is preserved — see
#: `s05_compose.merge_compose_preserving_user_edits`.
#:
#: n8n is included natively, with no flag behind it: it is not optional.
MANAGED_SERVICES = (
    POSTGRES_SERVICE,
    MATTERMOST_SERVICE,
    OLLAMA_SERVICE,
    FASTAPI_SERVICE,
    NGINX_SERVICE,
    WIREGUARD_SERVICE,
    BACKUP_SERVICE,
    N8N_SERVICE,
)

#: Services nginx names explicitly in its configuration and therefore
#: resolves by DNS only once, when it loads that configuration. Recreating
#: one of these leaves nginx pointing at a stale container IP — see
#: `s06_deploy.refresh_nginx`.
NGINX_UPSTREAM_SERVICES = frozenset({MATTERMOST_SERVICE})
