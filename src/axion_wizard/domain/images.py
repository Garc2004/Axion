"""Pinned Docker image tags — never `latest` (§6.4).

These are not user-configurable. If they were, an `axion.toml` carrying
`wireguard_image: ...:latest` would leave the wizard configuring a wg-easy it
does not know how to configure, and that failure is silent — the panel comes
up, and simply no credential ever works.

That pinned tag now points at wg-easy **v15**. v14 was configured through
`WG_HOST`/`PASSWORD_HASH`; v15 is a ground-up rewrite that ignores both and
is configured with `INIT_*` variables (see `templates/wg.env.j2`). The guard
below stays, inverted: it used to reject v15, now it rejects anything that
is not.
"""

from __future__ import annotations

WIREGUARD_IMAGE = "ghcr.io/wg-easy/wg-easy:15.3.0"
#: Stays on the 10.x line on purpose. 11 exists, but a major bump triggers
#: schema migrations against the database that do not come back: not an
#: upgrade an installer should apply on its own to a deployment with a message
#: history inside it.
#:
#: Current on its own line — 10.5.14 is the last patch 10.5 received — but be
#: clear about what that is worth: **10.5 is an ESR whose support ended on
#: 2025-11-15**, so nothing found after that date has been backported to it.
#: 10.11 ESR ends 2026-08-15, which buys nothing either. The supported
#: destination is 11.7 ESR (to 2027-05-15), and getting there is a deliberate
#: migration someone has to decide to run, not a pin change.
MATTERMOST_IMAGE = "mattermost/mattermost-team-edition:10.5.14"
#: Pinned to the 15 line for the same reason, and more so: changing major in
#: PostgreSQL requires `pg_upgrade` or a dump and restore, and the container
#: flatly refuses to start on a data directory from another version. Within
#: 15, staying current is worth it — those are security patches.
POSTGRES_IMAGE = "postgres:15.18-alpine"
NGINX_IMAGE = "nginx:1.31-alpine"
OLLAMA_IMAGE = "ollama/ollama:0.32.6"
#: The same Ollama version built against ROCm, for AMD GPUs. The default
#: image does not ship AMD's libraries: with it, handing over `/dev/kfd`
#: achieves nothing and the model keeps running on CPU without saying why.
OLLAMA_ROCM_IMAGE = "ollama/ollama:0.32.6-rocm"
BACKUP_IMAGE = "offen/docker-volume-backup:v2.48.2"
N8N_IMAGE = "docker.n8n.io/n8nio/n8n:2.34.4"

ALL_PINNED_IMAGES = (
    WIREGUARD_IMAGE,
    MATTERMOST_IMAGE,
    POSTGRES_IMAGE,
    NGINX_IMAGE,
    OLLAMA_IMAGE,
    OLLAMA_ROCM_IMAGE,
    BACKUP_IMAGE,
    N8N_IMAGE,
)


def ollama_image_for(gpu_acceleration: str) -> str:
    """The Ollama image matching the detected acceleration.

    NVIDIA and CPU share an image — NVIDIA's runtime injects the libraries
    from the host; AMD does not, it needs one built against ROCm. Intel does
    not appear because Ollama publishes no image for its GPUs: there is
    nothing to choose there and it runs on CPU.
    """
    from axion_wizard.detect.docker import GPU_ACCELERATION_ROCM

    return OLLAMA_ROCM_IMAGE if gpu_acceleration == GPU_ACCELERATION_ROCM else OLLAMA_IMAGE

#: The wg-easy repository and the range of major versions this wizard knows
#: how to configure; see `assert_wg_easy_tag_is_safe`.
#:
#: v14 and v15 share *nothing* of their configuration: v14 read
#: `WG_HOST`/`PASSWORD_HASH` (bcrypt) and v15 uses `INIT_*` with the password
#: in the clear, username included, and only on first boot. Supporting both
#: would mean maintaining two API clients and two `wg.env` templates for a
#: panel the user sees once; one is pinned instead.
WG_EASY_REPOSITORY = "ghcr.io/wg-easy/wg-easy"
WG_EASY_MIN_SAFE_MAJOR = 15
WG_EASY_MAX_SAFE_MAJOR = 15


class UnpinnedImageError(ValueError):
    """An image uses `latest` (or has no tag) instead of a pinned version."""


class UnsafeWgEasyTagError(ValueError):
    """wg-easy's effective tag is not the v15 this wizard knows how to configure."""


def split_image_tag(image: str) -> tuple[str, str | None]:
    """Split `image[:tag]`, respecting registries that carry a port.

    Splitting on the last `:` is not enough: in `localhost:5000/wg-easy` that
    `:` belongs to the registry's port, not to a tag. A tag never contains
    `/`, so if what is left on the right does, it was not a tag and the image
    really is unpinned.
    """
    repo, separator, candidate = image.rpartition(":")
    if not separator or "/" in candidate:
        return image, None
    return repo, candidate


def assert_image_is_pinned(image: str) -> None:
    """Raise `UnpinnedImageError` if `image` carries no explicit tag other
    than `latest`."""
    _repo, tag = split_image_tag(image)
    if tag is None:
        raise UnpinnedImageError(f"{image!r} has no tag — Docker will implicitly use 'latest'")
    if tag == "latest":
        raise UnpinnedImageError(f"{image!r} uses the 'latest' tag, forbidden by the spec (§6.4)")


def parse_wg_easy_major_version(tag: str) -> int | None:
    """Extract the major version from a wg-easy tag (`"15"`, `"15.3"`, `"v15.3.0"`)."""
    cleaned = tag.lstrip("v")
    major_str = cleaned.split(".", 1)[0]
    try:
        return int(major_str)
    except ValueError:
        return None


def assert_wg_easy_tag_is_safe(effective_tag: str) -> None:
    """Check the *effective* tag of the already-deployed wg-easy container.

    Each wg-easy major configures itself incompatibly with the last, and
    getting it wrong produces no error: the panel starts, it responds, and the
    only thing that happens is that the credentials the wizard configured do
    not work — or, on a v14, that the API it calls does not exist. Hence
    checking the running container's tag and not only the one written into
    `docker-compose.yml`, which anyone may have hand-edited.
    """
    if effective_tag == "latest":
        raise UnsafeWgEasyTagError(
            "wg-easy is running the 'latest' tag: whatever it points at today can "
            "stop being v15 without warning"
        )
    major = parse_wg_easy_major_version(effective_tag)
    if major is None:
        raise UnsafeWgEasyTagError(
            f"could not determine the major version of tag {effective_tag!r}"
        )
    if major < WG_EASY_MIN_SAFE_MAJOR:
        raise UnsafeWgEasyTagError(
            f"wg-easy {effective_tag} (v{major}) predates the v15 this wizard configures: "
            "v14 expects WG_HOST/PASSWORD_HASH and exposes a different API "
            "(/api/wireguard/client), so neither the credentials nor client enrolment "
            "would work"
        )
    if major > WG_EASY_MAX_SAFE_MAJOR:
        raise UnsafeWgEasyTagError(
            f"wg-easy {effective_tag} (v{major}) is newer than the v15 this wizard "
            "configures: each major changes its configuration entirely, and the failure "
            "would be silent — the panel starts and no credential works"
        )
