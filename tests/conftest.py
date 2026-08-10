"""Configuración común de los tests."""

import pytest

from axion_wizard.console import console, error_console

#: Ancho fijo para los paneles de Rich durante los tests.
#:
#: Sin esto, el ancho lo decide el entorno: Rich cae a 80 columnas cuando la
#: salida no es una terminal, y ahí una ruta larga —el `tmp_path` de pytest,
#: sin ir más lejos— se parte en varias líneas. Las aserciones del tipo
#: `assert "docker-compose.yml" in result.stderr` pasaban en Windows y
#: fallaban en Linux por la longitud del directorio temporal, que no tiene
#: nada que ver con lo que el test quiere comprobar.
TEST_CONSOLE_WIDTH = 200


@pytest.fixture(autouse=True, scope="session")
def _wide_consoles():
    """Fija el ancho de las consolas compartidas para toda la sesión."""
    console.width = TEST_CONSOLE_WIDTH
    error_console.width = TEST_CONSOLE_WIDTH
    yield
