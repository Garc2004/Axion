"""Parsing the JSON output of the Docker CLI.

`docker ps --format json`, `docker context ls --format json` and
`docker compose ps --format json` do not agree on the *shape* of what they
emit: depending on the subcommand and the CLI version, some return a whole
JSON array and others one JSON object per line. The same command has changed
shape between Docker versions, so assuming just one is not a simplification —
it is a latent bug that surfaces as "there are no containers", with no error.

This lived duplicated in three places (`detect.docker`, `services.compose`
and step 2), and the third copy only understood the line-per-object format,
so against a CLI that emitted an array the busy-port check under Docker
Desktop degraded silently.
"""

from __future__ import annotations

import json

__all__ = ["parse_json_lines_or_array"]


def parse_json_lines_or_array(output: str) -> list[dict]:
    """A list of JSON objects, whether `output` is an array or one object per
    line. Lines that fail to parse are dropped rather than aborting: Docker's
    output can carry interleaved warnings that are not JSON.
    """
    output = output.strip()
    if not output:
        return []

    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []

    entries: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries
