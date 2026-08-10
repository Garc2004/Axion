"""Configuración común de los tests."""

import pytest

from axion_wizard.render.console import console, error_console


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Ningún test corre con el directorio de trabajo real del repositorio.

    Sin esto, un test que invoca la CLI sin `--project-dir` y con mocking
    incompleto —falla antes de llegar a donde se pensaba, pero no antes de
    que algún paso ya haya escrito algo— deja archivos sueltos en la raíz del
    repo. Pasó de verdad: `test_no_elevate_flag_skips_elevation` y algún otro
    escribieron `axion/.axion-wizard-state.json` y `axion/nginx/certs/` en
    pleno directorio del proyecto, entre ejecuciones normales de la suite.

    Cada test que de verdad necesite una ruta concreta ya usa su propio
    `tmp_path`; aislar el cwd además no cambia nada para esos.
    """
    monkeypatch.chdir(tmp_path)

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
