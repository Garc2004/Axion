"""Parseo de la salida JSON de la CLI de Docker.

`docker ps --format json`, `docker context ls --format json` y
`docker compose ps --format json` no coinciden en la *forma* de lo que
emiten: según el subcomando y la versión de la CLI, unos devuelven un array
JSON completo y otros un objeto JSON por línea. El mismo comando cambió de
forma entre versiones de Docker, así que asumir una sola no es una
simplificación: es un bug latente que se manifiesta como "no hay
contenedores" sin ningún error.

Vivía duplicado en tres sitios —`detect.docker`, `services.compose` y el
paso 2— y la tercera copia solo entendía el formato por líneas, de modo que
con una CLI que emitiera un array la comprobación de puertos ocupados bajo
Docker Desktop degradaba en silencio.
"""

from __future__ import annotations

import json

__all__ = ["parse_json_lines_or_array"]


def parse_json_lines_or_array(output: str) -> list[dict]:
    """Lista de objetos JSON, venga `output` como array o como una línea por
    objeto. Las líneas que no parseen se descartan en vez de abortar: la
    salida de Docker puede traer avisos intercalados que no son JSON.
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
